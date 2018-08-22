from adapt.intent import IntentBuilder
from fuzzywuzzy import fuzz, process
from mycroft.skills.core import FallbackSkill, intent_file_handler, intent_handler
from mycroft.util.log import getLogger
import os

from requests.exceptions import (
    RequestException,
    Timeout,
    InvalidURL,
    URLRequired,
    SSLError,
    HTTPError)
from requests.packages.urllib3.exceptions import MaxRetryError

from .ha_client import HomeAssistantClient


__author__ = 'robconnolly, btotharye, nielstron'
LOGGER = getLogger(__name__)

# Timeout time for HA requests
TIMEOUT = 10


class HomeAssistantSkill(FallbackSkill):

    def __init__(self):
        super().__init__()
        self.ha = None
        self.enable_fallback = False

    @property
    def client(self):
        url = self.settings.get("url")
        password = self.settings.get("password")
        if url is not None:
            return HomeAssistantClient(url, password=password)
        else:
            token = os.environ.get('HASSIO_TOKEN')
            if token is not None:
                return HomeAssistantClient('http://hassio/homeassistant', password=token)

    def initialize(self):
        super().initialize()
        self.register_entity_file("temperature.entity")
        # Needs higher priority than general fallback skills
        self.register_fallback(self.handle_fallback, 2)

    # Try to find an entity on the HAServer
    # Creates dialogs for errors and speaks them
    # Returns None if nothing was found
    # Else returns entity that was found
    def _find_entity(self, entity, domains):
        self._setup()
        if self.ha is None:
            self.speak_dialog('homeassistant.error.setup')
            return False
        # TODO if entity is 'all', 'any' or 'every' turn on
        # every single entity not the whole group
        ha_entity = self._handle_client_exception(self.ha.find_entity,
                                                  entity, domains)
        if ha_entity is None:
            self.speak_dialog('homeassistant.device.unknown', data={
                              "dev_name": entity})
        return ha_entity

    # Calls passed method and catches often occurring exceptions
    def _handle_client_exception(self, callback, *args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except Timeout:
            self.speak_dialog('homeassistant.error.offline')
        except (InvalidURL, URLRequired, MaxRetryError) as e:
            self.speak_dialog('homeassistant.error.invalidurl', data={
                'url': e.request.url})
        except SSLError:
            self.speak_dialog('homeassistant.error.ssl')
        except HTTPError as e:
            # check if due to wrong password
            if e.response.status_code == 401:
                self.speak_dialog('homeassistant.error.wrong_password')
            else:
                self.speak_dialog('homeassistant.error.http', data={
                    'code': e.response.status_code,
                    'reason': e.response.reason})
        except (ConnectionError, RequestException) as exception:
            # TODO find a nice member of any exception to output
            self.speak_dialog('homeassistant.error', data={
                    'url': exception.request.url})
        return False

    @intent_handler(IntentBuilder("switchIntent").require(
        "SwitchActionKeyword").require("Action").require("Entity").build())
    def handle_switch_intent(self, message):
        LOGGER.debug("Starting Switch Intent")
        entity = message.data["Entity"]
        action = message.data["Action"]
        LOGGER.debug("Entity: %s" % entity)
        LOGGER.debug("Action: %s" % action)

        ha_entity = self._find_entity(
            entity,
            [
                'group',
                'light',
                'fan',
                'switch',
                'scene',
                'input_boolean',
                'climate'
            ]
        )
        if not ha_entity:
            return
        LOGGER.debug("Entity State: %s" % ha_entity['state'])
        ha_data = {'entity_id': ha_entity['id']}

        # IDEA: set context for 'turn it off' again or similar
        # self.set_context('Entity', ha_entity['dev_name'])

        if self.language == 'de':
            if action == 'ein':
                action = 'on'
            elif action == 'aus':
                action = 'off'
        if ha_entity['state'] == action:
            LOGGER.debug("Entity in requested state")
            self.speak_dialog('homeassistant.device.already', data={
                "dev_name": ha_entity['dev_name'], 'action': action})
        elif action == "toggle":
            self.ha.execute_service("homeassistant", "toggle",
                                    ha_data)
            if(ha_entity['state'] == 'off'):
                action = 'on'
            else:
                action = 'off'
            self.speak_dialog('homeassistant.device.%s' % action,
                              data=ha_entity)
        elif action in ["on", "off"]:
            self.speak_dialog('homeassistant.device.%s' % action,
                              data=ha_entity)
            self.ha.execute_service("homeassistant", "turn_%s" % action,
                                    ha_data)
        else:
            self.speak_dialog('homeassistant.error.sorry')
            return

    @intent_file_handler('set.light.brightness.intent')
    def handle_light_set_intent(self, message):
        entity = message.data["entity"]
        try:
            brightness_req = float(message.data["brightnessvalue"])
            if brightness_req > 100 or brightness_req < 0:
                self.speak_dialog('homeassistant.brightness.badreq')
        except KeyError:
            brightness_req = 10.0
        brightness_value = int(brightness_req / 100 * 255)
        brightness_percentage = int(brightness_req)
        LOGGER.debug("Entity: %s" % entity)
        LOGGER.debug("Brightness Value: %s" % brightness_value)
        LOGGER.debug("Brightness Percent: %s" % brightness_percentage)

        ha_entity = self._find_entity(entity, ['group', 'light'])
        if not ha_entity:
            return
        ha_data = {'entity_id': ha_entity['id']}

        # IDEA: set context for 'turn it off again' or similar
        # self.set_context('Entity', ha_entity['dev_name'])

        ha_data['brightness'] = brightness_value
        ha_data['dev_name'] = ha_entity['dev_name']
        self.ha.execute_service("homeassistant", "turn_on", ha_data)
        self.speak_dialog('homeassistant.brightness.dimmed',
                          data=ha_data)

        return

    @intent_handler(IntentBuilder("LightAdjBrightnessIntent") \
        .optionally("LightsKeyword") \
        .one_of("IncreaseVerb", "DecreaseVerb", "LightBrightenVerb",
                "LightDimVerb") \
        .require("Entity").optionally("BrightnessValue").build())
    def handle_light_adjust_intent(self, message):
        entity = message.data["Entity"]
        try:
            brightness_req = float(message.data["BrightnessValue"])
            if brightness_req > 100 or brightness_req < 0:
                self.speak_dialog('homeassistant.brightness.badreq')
        except KeyError:
            brightness_req = 10.0
        brightness_value = int(brightness_req / 100 * 255)
        # brightness_percentage = int(brightness_req) # debating use
        LOGGER.debug("Entity: %s" % entity)
        LOGGER.debug("Brightness Value: %s" % brightness_value)

        ha_entity = self._find_entity(entity, ['group', 'light'])
        if not ha_entity:
            return
        ha_data = {'entity_id': ha_entity['id']}
        # IDEA: set context for 'turn it off again' or similar
        # self.set_context('Entity', ha_entity['dev_name'])

        # if self.language == 'de':
        #    if action == 'runter' or action == 'dunkler':
        #        action = 'dim'
        #    elif action == 'heller' or action == 'hell':
        #        action = 'brighten'
        if "DecreaseVerb" in message.data or \
                "LightDimVerb" in message.data:
            if ha_entity['state'] == "off":
                self.speak_dialog('homeassistant.brightness.cantdim.off',
                                  data=ha_entity)
            else:
                light_attrs = self.ha.find_entity_attr(ha_entity['id'])
                if light_attrs['unit_measure'] is None:
                    print(ha_entity)
                    self.speak_dialog(
                        'homeassistant.brightness.cantdim.dimmable',
                        data=ha_entity)
                else:
                    ha_data['brightness'] = light_attrs['unit_measure']
                    if ha_data['brightness'] < brightness_value:
                        ha_data['brightness'] = 10
                    else:
                        ha_data['brightness'] -= brightness_value
                    self.ha.execute_service("homeassistant",
                                            "turn_on",
                                            ha_data)
                    ha_data['dev_name'] = ha_entity['dev_name']
                    self.speak_dialog('homeassistant.brightness.decreased',
                                      data=ha_data)
        elif "IncreaseVerb" in message.data or \
                "LightBrightenVerb" in message.data:
            if ha_entity['state'] == "off":
                self.speak_dialog(
                    'homeassistant.brightness.cantdim.off',
                    data=ha_entity)
            else:
                light_attrs = self.ha.find_entity_attr(ha_entity['id'])
                if light_attrs['unit_measure'] is None:
                    self.speak_dialog(
                        'homeassistant.brightness.cantdim.dimmable',
                        data=ha_entity)
                else:
                    ha_data['brightness'] = light_attrs['unit_measure']
                    if ha_data['brightness'] > brightness_value:
                        ha_data['brightness'] = 255
                    else:
                        ha_data['brightness'] += brightness_value
                    self.ha.execute_service("homeassistant",
                                            "turn_on",
                                            ha_data)
                    ha_data['dev_name'] = ha_entity['dev_name']
                    self.speak_dialog('homeassistant.brightness.increased',
                                      data=ha_data)
        else:
            self.speak_dialog('homeassistant.error.sorry')
            return

    @intent_handler(IntentBuilder("AutomationIntent").require(
        "AutomationActionKeyword").require("Entity").build())
    def handle_automation_intent(self, message):
        entity = message.data["Entity"]
        LOGGER.debug("Entity: %s" % entity)
        ha_entity = self._find_entity(
            entity,
            ['automation', 'scene', 'script']
        )

        if not ha_entity:
            return

        ha_data = {'entity_id': ha_entity['id']}

        # IDEA: set context for 'turn it off again' or similar
        # self.set_context('Entity', ha_entity['dev_name'])

        LOGGER.debug("Triggered automation/scene/script: {}".format(ha_data))
        if "automation" in ha_entity['id']:
            self.ha.execute_service('automation', 'trigger', ha_data)
            self.speak_dialog('homeassistant.automation.trigger',
                              data={"dev_name": ha_entity['dev_name']})
        elif "script" in ha_entity['id']:
            self.speak_dialog('homeassistant.automation.trigger',
                              data={"dev_name": ha_entity['dev_name']})
            self.ha.execute_service("homeassistant", "turn_on",
                                    data=ha_data)
        elif "scene" in ha_entity['id']:
            self.speak_dialog('homeassistant.device.on',
                              data=ha_entity)
            self.ha.execute_service("homeassistant", "turn_on",
                                    data=ha_data)

    @intent_handler(IntentBuilder("SensorIntent").require(
        "SensorStatusKeyword").require("Entity").build())
    def handle_sensor_intent(self, message):
        entity = message.data["Entity"]
        LOGGER.debug("Entity: %s" % entity)

        ha_entity = self._find_entity(entity, ['sensor'])
        if not ha_entity:
            return

        entity = ha_entity['id']

        # IDEA: set context for 'read it out again' or similar
        # self.set_context('Entity', ha_entity['dev_name'])

        unit_measurement = self.ha.find_entity_attr(entity)
        if unit_measurement['state'] is not None:
            sensor_unit = unit_measurement['unit_measure']
        else:
            sensor_unit = ''

        sensor_name = unit_measurement['name']
        sensor_state = unit_measurement['state']
        # extract unit for correct pronounciation
        # this is fully optional
        try:
            from quantulum import parser
            quantulumImport = True
        except ImportError:
            quantulumImport = False

        if quantulumImport and unit_measurement != '':
            quantity = parser.parse((u'{} is {} {}'.format(
                sensor_name, sensor_state, sensor_unit)))
            if len(quantity) > 0:
                quantity = quantity[0]
                if (quantity.unit.name != "dimensionless" and
                        quantity.uncertainty <= 0.5):
                    sensor_unit = quantity.unit.name
                    sensor_state = quantity.value

        self.speak_dialog('homeassistant.sensor', data={
            "dev_name": sensor_name,
            "value": sensor_state,
            "unit": sensor_unit})
        # IDEA: Add some context if the person wants to look the unit up
        # Maybe also change to name
        # if one wants to look up "outside temperature"
        # self.set_context("SubjectOfInterest", sensor_unit)

    # In progress, still testing.
    # Device location works.
    # Proximity might be an issue
    # - overlapping command for directions modules
    # - (e.g. "How far is x from y?")
    @intent_handler(IntentBuilder("TrackerIntent").require(
        "DeviceTrackerKeyword").require("Entity").build())
    def handle_tracker_intent(self, message):
        entity = message.data["Entity"]
        LOGGER.debug("Entity: %s" % entity)

        ha_entity = self._find_entity(entity, ['device_tracker'])
        if not ha_entity:
            return

        # IDEA: set context for 'locate it again' or similar
        # self.set_context('Entity', ha_entity['dev_name'])

        entity = ha_entity['id']
        dev_name = ha_entity['dev_name']
        dev_location = ha_entity['state']
        self.speak_dialog('homeassistant.tracker.found',
                          data={'dev_name': dev_name,
                                'location': dev_location})

    @intent_file_handler('set.temperature.intent')
    def handle_set_temperature_intent(self, message):
        name = message.data.get("name")
        temperature = message.data["temperature"]
        entities = self.client.find_entities(domain='climate', name=name)
        if entities == []:
            if name is not None:
                return self.speak_dialog("no.thermostat.by.name", data={name: name})
            else:
                return self.speak_dialog("no.thermostat")
        else:
            target = entities[0]
            data = {'temperature': temperature}
            if name is None:
                self.client.execute_service('climate', 'set_temperature', data)
                self.speak_dialog("set.temperature", data)
            else:
                target = entities[0]
                data['entity_id'] = target['entity_id']
                self.client.execute_service('climate', 'set_temperature', data)
                data["name"] = target['attributes'].get('friendly_name', thermostat['entity_id'])
                self.speak_dialog("set.temperature.by.name", data)

    def handle_fallback(self, message):
        if not self.enable_fallback:
            return False
        self._setup()
        if self.ha is None:
            self.speak_dialog('homeassistant.error.setup')
            return False
        # pass message to HA-server
        response = self._handle_client_exception(
            self.ha.engage_conversation,
            message.data.get('utterance'))
        if not response:
            return False
        # default non-parsing answer: "Sorry, I didn't understand that"
        answer = response.get('speech')
        if not answer or answer == "Sorry, I didn't understand that":
            return False

        asked_question = False
        # TODO: maybe enable conversation here if server asks sth like
        # "In which room?" => answer should be directly passed to this skill
        if answer.endswith("?"):
            asked_question = True
        self.speak(answer, expect_response=asked_question)
        return True

    def shutdown(self):
        self.remove_fallback(self.handle_fallback)
        super(HomeAssistantSkill, self).shutdown()

    def stop(self):
        pass


def create_skill():
    return HomeAssistantSkill()
