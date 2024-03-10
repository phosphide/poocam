#!/usr/bin/env python3
import json
import time
import logging
import click
from gpiozero import Button
import paho.mqtt.client as mqtt


mqtt_prefix = "homeassistant"

debounce_time = 0.1


class MQTTDevice:
    def __init__(self, device_name, mqtt_client: mqtt.Client):
        self.device_name = device_name
        self.mqtt_client = mqtt_client
        self.config_topic = f"{mqtt_prefix}/binary_sensor/{device_name}/config"
        self.availability_topic = f"{mqtt_prefix}/binary_sensor/{device_name}/availability"
        self.state_topic = f"{mqtt_prefix}/binary_sensor/{device_name}/state"

        self.config_payload = {"component": "binary_sensor", "device_class": "door", "name": self.device_name,
                               "state_topic": self.state_topic, "availability_topic": self.availability_topic,
                               "payload_off": "off", "payload_on": "on"}

        self.state = None

    def publish_availability(self, available: bool):
        self.mqtt_client.publish(self.availability_topic, "online" if available else "offline", qos=1)

    def publish_state(self):
        if self.state is not None:
            self.mqtt_client.publish(self.state_topic, "on" if self.state else "off", qos=1, retain=False)

    def set_state(self, state: bool):
        self.state = state
        if self.mqtt_client.is_connected():
            self.publish_state()

    def on_connect(self):
        self.mqtt_client.publish(self.config_topic, json.dumps(self.config_payload), qos=1)
        self.mqtt_client.publish(self.availability_topic, "online", qos=1)
        self.publish_state()


@click.command()
@click.option("-h", "--host", default="localhost", help="MQTT broker address")
@click.option("-c", "--credentials", help="MQTT credentials JSON file", type=click.File("r"))
@click.option("-v", "--verbose", help="Verbosity level", count=True)
def main(host, credentials, verbose):
    loglevel = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}.get(verbose, logging.DEBUG)

    logging.basicConfig(level=loglevel)

    last_published_state = None
    last_published_time = -float("inf")

    reed = Button(23, pull_up=True)

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    mqtt_sensor = MQTTDevice("Drzwi", mqtt_client)

    mqtt_client.enable_logger()
    if credentials:
        credentials_dict = json.load(credentials)
        mqtt_client.username = credentials_dict["username"]
        mqtt_client.password = credentials_dict["password"]

    mqtt_client.will_set(mqtt_sensor.availability_topic, "offline", qos=1)
    mqtt_client.connect(host=host)

    def on_connect(_client: mqtt.Client, _userdata, _flags, reason_code, _properties):
        if not reason_code.is_failure:
            mqtt_sensor.on_connect()

    mqtt_client.on_connect = on_connect

    mqtt_client.loop_start()

    while True:
        state = not bool(reed.value)
        if state != last_published_state:
            if time.monotonic() > last_published_time + debounce_time:
                logging.info(f"New state: {state}")
                mqtt_sensor.set_state(state)
                last_published_time = time.monotonic()
                last_published_state = state
            else:
                logging.debug(f"Debounce")
        time.sleep(1 / 20)


if __name__ == "__main__":
    main()
