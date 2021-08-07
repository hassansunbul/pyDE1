"""
Copyright © 2021 Jeff Kletsky. All Rights Reserved.

License for this software, part of the pyDE1 package, is granted under
GNU General Public License v3.0 only
SPDX-License-Identifier: GPL-3.0-only

Replays the captured packets from a flow sequence
such as for testing or demonstration of "GUIs"

NB: Does _not_ modify the DB contents, so real-time pulls by the consumer
    will be "at completion", not "as they were".

    This primarily impacts the sequence table, which is updated
    as the sequence unfolds.
"""

import asyncio
import json
import logging
import os
import socket
import sqlite3
import time

from typing import NamedTuple, Union, List

import paho.mqtt.client as mqtt
from paho.mqtt.client import MQTTv5, MQTT_CLEAN_START_FIRST_ONLY

MQTT_TOPIC_ROOT = 'KEpyDE1'
MQTT_CLIENT_ID = f"replay@{socket.gethostname()}[{os.getpid()}]"
MQTT_BROKER_HOSTNAME = '::'
MQTT_BROKER_PORT = 1883
MQTT_TRANSPORT = 'tcp'
MQTT_TLS_CONTEXT = None
MQTT_KEEPALIVE = 60
MQTT_USERNAME = None
MQTT_PASSWORD = None

DB_DIR = '/var/lib/pyDE1'

database_uri = f"file:{os.path.join(DB_DIR, 'pyDE1.sqlite3')}?mode=ro"

logger = logging.getLogger()
format_string = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.DEBUG, format=format_string)

client_logger = logging.getLogger('MQTT')
client_logger.level = logging.ERROR


# TODO: Figure out how not to duplicate this in so many places

# NB: Remember to reconstruct the class element

class SequenceRow (NamedTuple):
    id: str
    active_state:   str
    start_sequence: float
    start_flow:     float
    end_flow:       float
    end_sequence:   float
    profile_id:     str
    # https://www.sqlite.org/quirks.html#no_separate_boolean_datatype
    profile_assumed:    int     # 0: False, 1: True
    resource_version:                           str
    resource_de1_id:                            str
    resource_de1_read_once:                     str
    resource_de1_calibration_flow_multiplier:   str
    resource_de1_control_mode:                  str
    resource_de1_control_tank_water_threshold:  str
    resource_de1_setting_before_flow:           str
    resource_de1_setting_steam:                 str
    resource_de1_setting_target_group_temp:     str
    resource_scale_id:                          str

    @property
    def class_str(self):
        return None


def sequence_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return SequenceRow(*row)


class ShotSampleRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    de1_time:           float
    sample_time:        int
    group_pressure:     float
    group_flow:         float
    mix_temp:           float
    head_temp:          float
    set_mix_temp:       float
    set_head_temp:      float
    set_group_pressure: float
    set_group_flow:     float
    frame_number:       int
    steam_temp:         float
    volume_preinfuse:   float
    volume_pour:        float
    volume_total:       float
    volume_by_frames:   str     # representation of list

    @property
    def class_str(self):
        return 'ShotSampleWithVolumesUpdate'


def shot_sample_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return ShotSampleRow(*row)


class WeightFlowRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    scale_time:             float
    current_weight:         float
    current_weight_time:    float
    average_flow:           float
    average_flow_time:      float
    median_weight:          float
    median_weight_time:     float
    median_flow:            float
    median_flow_time:       float

    @property
    def class_str(self):
        return 'WeightAndFlowUpdate'


def weight_flow_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return WeightFlowRow(*row)


class StateUpdateRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    event_time:         float
    state:              str
    substate:           str
    previous_state:     str
    previous_substate:  str
    is_error_state:     str     # TODO: Fix this in schema and access

    @property
    def class_str(self):
        return 'StateUpdate'


def state_update_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return StateUpdateRow(*row)


class SequencerGateNotificationRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    name:           str
    action:         str
    active_state:   str

    @property
    def class_str(self):
        return 'SequencerGateNotification'


def sequence_gate_notification_row_factory(cur: sqlite3.Cursor,
                                           row: sqlite3.Row):
    return SequencerGateNotificationRow(*row)


class WaterLevelRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    level:              float
    start_fill_level:   float

    @property
    def class_str(self):
        return 'WaterLevelUpdate'


def water_level_row_factory(cur: sqlite3.Cursor, row: sqlite3.Row):
    return WaterLevelRow(*row)


# NB: Database does not yet not include name or ID

class OldConnectivityChangeRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    state:              str
    # name:               str
    # id:                 str


def old_connectivity_change_row_factory(cur: sqlite3.Cursor,
                                        row: sqlite3.Row):
    return OldConnectivityChangeRow(*row)


class ConnectivityChangeRow (NamedTuple):
    version:            str
    sender:             str
    arrival_time:       float
    create_time:        float
    event_time:         float
    #
    state:              str
    name:               str
    id:                 str

    @property
    def class_str(self):
        return 'ConnectivityChange'


def augment_old_connectivity_change_row(
        old_row: OldConnectivityChangeRow) -> ConnectivityChangeRow:

    if old_row.sender == 'DE1':
        id = 'D9:B2:48:aa:bb:cc'
        name = 'DE1'
    else:
        id = 'CF:75:75:aa:bb::cc'
        name = 'Skale'

    return ConnectivityChangeRow(*old_row, name, id)


class SendListEntry (NamedTuple):
    send_at:    float
    payload:    str


def _shift_if_time(key: str, val: Union[str, float], shift: float):
    if key.endswith('_time'):
        return val + shift
    else:
        return val


def create_entry(row: NamedTuple, shift_time: float) -> SendListEntry:
    """
    Shift all time elements by adding shift_time
    Return a JSON string, so compatible with api/mqtt/run
        outbound_pipe_reader()
            item_json = outbound_pipe.recv()
    """

    row_dict = {k:_shift_if_time(k, v, shift_time)
                for (k,v) in row._asdict().items()}
    # For now, bomb out on missing property
    row_dict['class'] = row.class_str
    row_dict['shifted'] = shift_time

    return SendListEntry(send_at=row_dict['event_time'],
                         payload=json.dumps(row_dict))


def collect_send_list(sequence_id: str,
                      shift_time: float) -> List[SendListEntry]:
    send_list = []

    with sqlite3.connect(database_uri, uri=True) as db:

        db.row_factory = shot_sample_row_factory
        cur = db.execute(f"SELECT {' ,'.join(ShotSampleRow._fields)} "
                         "FROM shot_sample_with_volume_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = weight_flow_row_factory
        cur = db.execute(f"SELECT {' ,'.join(WeightFlowRow._fields)} "
                         "FROM weight_and_flow_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = state_update_row_factory
        cur = db.execute(f"SELECT {' ,'.join(StateUpdateRow._fields)} "
                         "FROM state_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = water_level_row_factory
        cur = db.execute(f"SELECT {' ,'.join(WaterLevelRow._fields)} "
                         "FROM water_level_update "
                         "WHERE sequence_id == :id "
                         "ORDER BY event_time",
                         {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

        db.row_factory = old_connectivity_change_row_factory
        cur = db.execute(
            f"SELECT {' ,'.join(OldConnectivityChangeRow._fields)} "
            "FROM connectivity_change "
            "WHERE sequence_id == :id "
            "ORDER BY event_time",
            {'id': sequence_id})
        for row in cur.fetchall():
            new_row = augment_old_connectivity_change_row(row)
            send_list.append(create_entry(new_row, shift_time))

        db.row_factory = sequence_gate_notification_row_factory
        cur = db.execute(
            f"SELECT {' ,'.join(SequencerGateNotificationRow._fields)} "
            "FROM sequencer_gate_notification "
            "WHERE sequence_id == :id "
            "ORDER BY event_time",
            {'id': sequence_id})
        for row in cur.fetchall():
            send_list.append(create_entry(row, shift_time))

    send_list.sort(key=lambda entry: entry.send_at)
    return send_list


def get_sequence_start_time(sequence_id: str) -> float:

    with sqlite3.connect(database_uri, uri=True) as db:
        db.row_factory = sequence_row_factory
        cur = db.execute(f"SELECT {', '.join(SequenceRow._fields)} "
                         "FROM sequence "
                         "WHERE id == :id",
                         (sequence_id,))
        row = cur.fetchone()
        return row.start_sequence

# MQTT


def on_log_callback(client: mqtt.Client, userdata, level, buf):
    client_logger.info(f"CB: Log: level: {level} '{buf}' ({type(buf)})")

def on_connect_callback(client, userdata, flags, reasonCode, properties):
    client_logger.info(f"CB: Connect: flags: {flags}, reasonCode: {reasonCode}, "
                f"properties {properties}")

def on_publish_callback(client, userdata, mid):
    client_logger.info(f"CB: Published: mid: {mid}")

# Caught exception in on_disconnect:
#     on_disconnect_callback() missing 1 required positional argument:
#         'properties'
def on_disconnect_callback(client, userdata, reasonCode, properties=None):
    client_logger.info(f"CB: Disconnect: reasonCode: {reasonCode}, "
                f"properties {properties}")

def on_socket_open_callback(client, userdata, socket):
    client_logger.info(f"CB: Socket open: socket: {socket}")

def on_socket_close_callback(client, userdata, socket):
    client_logger.info(f"CB: Socket close: socket: {socket}")

def on_socket_register_write_callback(client, userdata, socket):
    client_logger.info(f"CB: Socket register write: socket: {socket}")

def on_socket_unregister_write_callback(client, userdata, socket):
    client_logger.info(f"CB: Socket unregister write: socket: {socket}")

mqtt_client = mqtt.Client(
    client_id=MQTT_CLIENT_ID,
    clean_session=None,  # Required for MQTT5
    userdata=None,
    protocol=MQTTv5,
    transport=MQTT_TRANSPORT,
)

# mqtt_client.on_log = on_log_callback
mqtt_client.on_connect = on_connect_callback
# mqtt_client.on_publish = on_publish_callback
mqtt_client.on_disconnect = on_disconnect_callback
mqtt_client.on_socket_open = on_socket_open_callback
mqtt_client.on_socket_close = on_socket_close_callback
# mqtt_client.on_socket_register_write = on_socket_register_write_callback
# mqtt_client.on_socket_unregister_write = on_socket_unregister_write_callback

mqtt_client.enable_logger(client_logger)

mqtt_client.connect(host=MQTT_BROKER_HOSTNAME,
               port=MQTT_BROKER_PORT,
               keepalive=MQTT_KEEPALIVE,
               bind_address="",
               bind_port=0,
               clean_start=MQTT_CLEAN_START_FIRST_ONLY,
               properties=None)



if __name__ == '__main__':

    id = '87f17aa1-ea0a-41e7-aac0-fd042f9729db'

    sst = get_sequence_start_time(id)
    now = time.time()
    start_sequence_at = now + 5
    shift_time = start_sequence_at - sst
    send_list = collect_send_list(id, shift_time)
    mqtt_client.loop_start()

    MQTT_LEAD_TIME = 0.000  # seconds
    while len(send_list):
        next_to_send = send_list.pop(0)
        while next_to_send.send_at > time.time() + MQTT_LEAD_TIME:
            time.sleep(0.010)
        print(time.time(), next_to_send)
        item_as_dict = json.loads(next_to_send.payload)
        topic = f"{MQTT_TOPIC_ROOT}/{item_as_dict['class']}"
        mqtt_client.publish(
            topic=topic,
            payload=next_to_send.payload,
            qos=0,
            retain=False,
            properties=None
        )

    # Have to let the last message drain before existing
    time.sleep(1)




