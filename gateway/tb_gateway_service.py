#     Copyright 2021. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

from sys import getsizeof, executable, argv
from os import listdir, path, execv, pathsep, system
from time import time, sleep
import logging
import logging.config
import logging.handlers
from queue import Queue
from random import choice
from string import ascii_lowercase
from threading import Thread, RLock

from yaml import safe_load
from simplejson import load, dumps, loads

from tb_utility.tb_loader import TBModuleLoader
from tb_utility.tb_utility import TBUtility
from gateway.tb_client import TBClient
from tb_utility.tb_logger import TBLoggerHandler
from storage.memory_event_storage import MemoryEventStorage
from storage.file_event_storage import FileEventStorage


log = logging.getLogger('service')
main_handler = logging.handlers.MemoryHandler(-1)

DEFAULT_CONNECTORS = {
            "mqtt": "MqttConnector",
            "modbus": "ModbusConnector",
            "opcua": "OpcUaConnector",
            "ble": "BLEConnector",
            "request": "RequestConnector",
            "can": "CanConnector",
            "bacnet": "BACnetConnector",
            "odbc": "OdbcConnector",
            "rest": "RESTConnector",
            "snmp": "SNMPConnector",
        }

class TBGatewayService:
    def __init__(self, config_file=None):
        self.stopped = False
        if config_file is None:
            config_file = path.dirname(path.dirname(path.abspath(__file__))) + '/config/tb_gateway.yaml'.replace('/', path.sep)
        with open(config_file) as general_config:
            self.__config = safe_load(general_config)
        self._config_dir = path.dirname(path.abspath(config_file)) + path.sep
        logging_error = None
        try:
            logging.config.fileConfig(self._config_dir + "logs.conf", disable_existing_loggers=False)
        except Exception as e:
            logging_error = e
        global log
        log = logging.getLogger('service')
        log.info("Gateway starting...")
        self.available_connectors = {}
        self.__connector_incoming_messages = {}
        self.__connected_devices = {}
        self.__saved_devices = {}
        self.name = ''.join(choice(ascii_lowercase) for _ in range(64))
        self.__connected_devices_file = "connected_devices.json"
        self.tb_client = TBClient(self.__config["thingsboard"])
        self.tb_client.connect()
        self.subscribe_to_required_topics()
        self.__subscribed_to_rpc_topics = True
        if logging_error is not None:
            TBLoggerHandler.set_default_handler()
        self.counter = 0
        self.__rpc_reply_sent = False
        global main_handler
        self.main_handler = main_handler
        self.remote_handler = TBLoggerHandler(self)
        self.main_handler.setTarget(self.remote_handler)
        self._default_connectors = DEFAULT_CONNECTORS
        self._implemented_connectors = {}
        self._event_storage_types = {
            "memory": MemoryEventStorage,
            "file": FileEventStorage,
        }
        self._event_storage = self._event_storage_types[self.__config["storage"]["type"]](self.__config["storage"])
        self.connectors_configs = {}
        self._load_connectors()
        self._connect_with_connectors()
        self.__load_persistent_devices()
        self._published_events = Queue(-1)
        self._send_thread = Thread(target=self.__read_data_from_storage, daemon=True,
                                   name="Send data to Thingsboard Thread")
        self._send_thread.start()
        log.info("Gateway started.")

        try:
            while not self.stopped:
                if not self.tb_client.is_connected() and self.__subscribed_to_rpc_topics:
                    self.__subscribed_to_rpc_topics = False
                if self.tb_client.is_connected() and not self.__subscribed_to_rpc_topics:
                    for device in self.__saved_devices:
                        self.add_device(device, {"connector": self.__saved_devices[device]["connector"]}, True, device_type=self.__saved_devices[device]["device_type"])
                    self.subscribe_to_required_topics()
                    self.__subscribed_to_rpc_topics = True
                else:
                    try:
                        sleep(.1)
                    except Exception as e:
                        log.exception(e)
                        break
        except KeyboardInterrupt:
            self.__stop_gateway()
        except Exception as e:
            log.exception(e)
            self.__stop_gateway()
            self.__close_connectors()
            log.info("The gateway has been stopped.")
            self.tb_client.stop()

    def __close_connectors(self):
        for current_connector in self.available_connectors:
            try:
                self.available_connectors[current_connector].close()
                log.debug("Connector %s closed connection.", current_connector)
            except Exception as e:
                log.exception(e)

    def __stop_gateway(self):
        self.stopped = True
        log.info("Stopping...")
        self.__close_connectors()
        log.info("The gateway has been stopped.")
        self.tb_client.disconnect()
        self.tb_client.stop()

    def subscribe_to_required_topics(self):
        self.tb_client.client.set_server_side_rpc_request_handler(self._rpc_request_handler)

    def _load_connectors(self):
        self.connectors_configs = {}
        if self.__config.get("connectors"):
            for connector in self.__config['connectors']:
                try:
                    connector_class = TBModuleLoader.import_module(connector["type"], self._default_connectors.get(connector["type"], connector.get("class")))
                    self._implemented_connectors[connector["type"]] = connector_class
                    with open(self._config_dir + connector['configuration'], 'r', encoding="UTF-8") as conf_file:
                        connector_conf = load(conf_file)
                        if not self.connectors_configs.get(connector['type']):
                            self.connectors_configs[connector['type']] = []
                        connector_conf["name"] = connector["name"]
                        self.connectors_configs[connector['type']].append({"name": connector["name"], "config": {connector['configuration']: connector_conf}})
                except Exception as e:
                    log.error("Error on loading connector:")
                    log.exception(e)

    def _connect_with_connectors(self):
        for connector_type in self.connectors_configs:
            for connector_config in self.connectors_configs[connector_type]:
                for config in connector_config["config"]:
                    connector = None
                    try:
                        if connector_config["config"][config] is not None:
                            if self._implemented_connectors[connector_type]:
                                connector = self._implemented_connectors[connector_type](self, connector_config["config"][config],
                                                                                        connector_type)
                                connector.setName(connector_config["name"])
                                self.available_connectors[connector.get_name()] = connector
                                connector.open()
                            else:
                                log.warning("Connector implementation not found for %s", connector_config["name"])
                        else:
                            log.info("Config not found for %s", connector_type)
                    except Exception as e:
                        log.exception(e)
                        if connector is not None:
                            connector.close()

    def send_to_storage(self, connector_name, data):
        #print("[TBGatewayService send_to_storage]",connector_name,data)
        if not connector_name == self.name:
            if not TBUtility.validate_converted_data(data):
                log.error("Data from %s connector is invalid.", connector_name)
                return None
            if data["deviceName"] not in self.get_devices():
                self.add_device(data["deviceName"],
                                {"connector": self.available_connectors[connector_name]}, wait_for_publish=True, device_type=data["deviceType"])
            if not self.__connector_incoming_messages.get(connector_name):
                self.__connector_incoming_messages[connector_name] = 0
            else:
                self.__connector_incoming_messages[connector_name] += 1
        else:
            data["deviceName"] = "currentThingsBoardGateway"

        telemetry = {}
        telemetry_with_ts = []
        for item in data["telemetry"]:
            if item.get("ts") is None:
                telemetry = {**telemetry, **item}
            else:
                telemetry_with_ts.append({"ts": item["ts"], "values": {**item["values"]}})
        if telemetry_with_ts:
            data["telemetry"] = telemetry_with_ts
        else:
            data["telemetry"] = {"ts": int(time() * 1000), "values": telemetry}

        json_data = dumps(data)
        #print("[TBGatewayService send_to_storage json_data]",json_data)
        save_result = self._event_storage.put(json_data)
        if not save_result:
            log.error('Data from the device "%s" cannot be saved, connector name is %s.',
                      data["deviceName"],
                      connector_name)

    def __read_data_from_storage(self):
        devices_data_in_event_pack = {}
        log.debug("Send data Thread has been started successfully.")
        while True:
            try:
                if self.tb_client.is_connected():
                    size = getsizeof(devices_data_in_event_pack)
                    events = []
                    # if self.__remote_configurator is None or not self.__remote_configurator.in_process:
                    events = self._event_storage.get_event_pack()
                    if events:
                        #print("[TBGatewayService __read_data_from_storage events]",events)
                        for event in events:
                            self.counter += 1
                            try:
                                current_event = loads(event)
                            except Exception as e:
                                log.exception(e)
                                continue
                            #print("[TBGatewayService __read_data_from_storage current_event]",current_event)
                            devices_data_in_event_pack = {"schemaId":"","udoi":"","content":{}}
                            devices_data_in_event_pack["udoi"]=current_event["udoi"]
                            devices_data_in_event_pack["schemaId"]=current_event["schemaId"]
                            devices_data_in_event_pack["content"].update(current_event["telemetry"]["values"])
                        if devices_data_in_event_pack:
                            if not self.tb_client.is_connected():
                                continue
                            while self.__rpc_reply_sent:
                                sleep(.01)
                            #print("[TBGatewayService __read_data_from_storage devices_data_in_event_pack2]",devices_data_in_event_pack)
                            self.__send_data(devices_data_in_event_pack)
                        if self.tb_client.is_connected():
                            #print("===============self.tb_client.is_connected() and (self.__remote_configurator is None or not self.__remote_configurator.in_process):")
                            success = True
                            while not self._published_events.empty():
                                if not self.tb_client.is_connected() or self._published_events.empty() or self.__rpc_reply_sent:
                                    success = False
                                    break
                                event = self._published_events.get(False, 10)
                                try:
                                    if self.tb_client.is_connected():
                                        if self.tb_client.client.quality_of_service == 1:
                                            success = event.get() == event.TB_ERR_SUCCESS
                                        else:
                                            success = True
                                    else:
                                        break
                                except Exception as e:
                                    log.exception(e)
                                    success = False
                                sleep(.01)
                            if success:
                                self._event_storage.event_pack_processing_done()
                                del devices_data_in_event_pack
                                devices_data_in_event_pack = {}
                        else:
                            continue
                    else:
                        sleep(.01)
                else:
                    sleep(.1)
            except Exception as e:
                log.exception(e)
                sleep(1)

    def __send_data(self, devices_data_in_event_pack):
        #print("[TBGatewayService __send_data devices_data_in_event_pack]",devices_data_in_event_pack)
        try:
            self._published_events.put(self.tb_client.client.gw_send_payload(devices_data_in_event_pack))
            devices_data_in_event_pack = {"schemaId":"","udoi":"","content":{}}
        except Exception as e:
            log.exception(e)

    def _rpc_request_handler(self, request_id, content):
        # print("[TBGatewayService _rpc_request_handler]", request_id, content)
        try:
            device = content.get("device")
            if device is not None:
                connector_name = self.get_devices()[device].get("connector")
                #print("[TBGatewayService _rpc_request_handler self.__connected_devices]", self.__connected_devices)
                if connector_name is not None:
                    connector_name.server_side_rpc_handler(content)
                else:
                    log.error("Received RPC request but connector for the device %s not found. Request data: \n %s",
                              content["device"],
                              dumps(content))
            else:
                log.error("Received RPC request does not have the param of device. Request data: \n %s", dumps(content))
        except Exception as e:
            log.exception(e)

    def add_device(self, device_name, content, wait_for_publish=False, device_type=None):
        if device_name not in self.__saved_devices:
            device_type = device_type if device_type is not None else 'default'
            self.__connected_devices[device_name] = {**content, "device_type": device_type}
            self.__saved_devices[device_name] = {**content, "device_type": device_type}
            self.__save_persistent_devices()
        if wait_for_publish:
            self.tb_client.client.gw_connect_device(device_name, device_type).wait_for_publish()
        else:
            self.tb_client.client.gw_connect_device(device_name, device_type)

    def get_devices(self):
        return self.__connected_devices

    def __load_persistent_devices(self):
        devices = {}
        if self.__connected_devices_file in listdir(self._config_dir) and \
                path.getsize(self._config_dir + self.__connected_devices_file) > 0:
            try:
                with open(self._config_dir + self.__connected_devices_file) as devices_file:
                    devices = load(devices_file)
            except Exception as e:
                log.exception(e)
        else:
            connected_devices_file = open(self._config_dir + self.__connected_devices_file, 'w')
            connected_devices_file.close()

        if devices is not None:
            log.debug("Loaded devices:\n %s", devices)
            for device_name in devices:
                try:
                    if self.available_connectors.get(devices[device_name]):
                        self.__connected_devices[device_name] = {
                            "connector": self.available_connectors[devices[device_name]]}
                except Exception as e:
                    log.exception(e)
                    continue
        else:
            log.debug("No device found in connected device file.")
            self.__connected_devices = {} if self.__connected_devices is None else self.__connected_devices

    def __save_persistent_devices(self):
        with open(self._config_dir + self.__connected_devices_file, 'w') as config_file:
            try:
                data_to_save = {}
                for device in self.__connected_devices:
                    if self.__connected_devices[device]["connector"] is not None:
                        data_to_save[device] = self.__connected_devices[device]["connector"].get_name()
                config_file.write(dumps(data_to_save, indent=2, sort_keys=True))
            except Exception as e:
                log.exception(e)
        log.debug("Saved connected devices.")


if __name__ == '__main__':
    TBGatewayService(path.dirname(path.dirname(path.abspath(__file__))) + '/config/tb_gateway.yaml'.replace('/', path.sep))
