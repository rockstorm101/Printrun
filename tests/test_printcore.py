import pytest
import printrun.printcore as printcore
import serial
import threading
import logging
import time
import random
import os

DEFAULT_ANSWER = 'ok:'
CNC_PROCESS_TIME = 0.2 # in s
def slow_cnc():
    time.sleep(random.randrange(0,CNC_PROCESS_TIME*100,1)/100)
    return DEFAULT_ANSWER.encode()

@pytest.fixture
def mocked_serial_port(mocker):
    mocker.patch('serial.Serial.open')
    mocker.patch('serial.Serial.write')
    mocker.patch('serial.Serial.in_waiting')
    mocker.patch('serial.Serial.readline', side_effect = slow_cnc)
    mocker.patch('serial.Serial.close')
    return mocker

@pytest.fixture
def dced_core(): # disconnected core
    core = printcore.Printcore()
    return core

@pytest.fixture
def conn_core(mocked_serial_port, dced_core):
    dced_core.connect('/mocked/port', 100000)
    yield dced_core
    dced_core.disconnect()
    time.sleep(dced_core.check_interval * 1.1)

@pytest.fixture(scope="session")
def gcode_file():
    file = open('test.gcode', 'w')
    for i in range(0, 100):
        file.write('G' + str(i) + '\n')
    file.close()
    file = open('test.gcode', 'r')
    yield file
    file.close()
    os.remove('test.gcode')

"""
Test connect
"""
def test_connect_calls_serial_open(dced_core, mocked_serial_port):
    dced_core.connect('/mocked/port', 100000)
    serial.Serial.open.assert_called_once()
    assert dced_core.is_connected()
    dced_core.disconnect()
    time.sleep(CNC_PROCESS_TIME * 1.1)

def test_connect_raises_connection_error(dced_core):
    with pytest.raises(ConnectionError):
        dced_core.connect('/non-existent/port', 100000)
    assert not dced_core.is_connected()

"""
Test disconnect
"""
@pytest.mark.timeout(CNC_PROCESS_TIME * 3)
def test_disconnect(dced_core, mocked_serial_port):
    dced_core.connect('/mocked/port', 100000)
    dced_core.disconnect()
    time.sleep(CNC_PROCESS_TIME * 1.1)
    serial.Serial.close.assert_called_once()
    assert not dced_core.is_connected()
    assert threading.active_count() <= 1

@pytest.mark.timeout(CNC_PROCESS_TIME * 4)
def test_disconnect_while_working(dced_core, mocked_serial_port, gcode_file):
    dced_core.connect('/mocked/port', 100000)
    dced_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    dced_core.disconnect()
    time.sleep(CNC_PROCESS_TIME * 1.1)
    serial.Serial.close.assert_called_once()
    assert not dced_core.is_connected()
    assert threading.active_count() <= 1

@pytest.mark.timeout(CNC_PROCESS_TIME * 5)
def test_disconnect_while_paused(dced_core, mocked_serial_port, gcode_file):
    dced_core.connect('/mocked/port', 100000)
    dced_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    dced_core.pause()
    time.sleep(CNC_PROCESS_TIME)
    dced_core.disconnect()
    time.sleep(CNC_PROCESS_TIME * 1.1)
    serial.Serial.close.assert_called_once()
    assert not dced_core.is_connected()
    assert threading.active_count() <= 1

@pytest.mark.timeout(CNC_PROCESS_TIME * 2)
def test_send_now_while_not_working(conn_core):
    command = "Random Command"
    answer = conn_core.send_now(command)
    serial.Serial.write.assert_called_once_with((command + "\n").encode())
    assert answer == DEFAULT_ANSWER

@pytest.mark.timeout(CNC_PROCESS_TIME * 4)
def test_send_now_while_working(conn_core, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    command = "Random Command"
    answer = conn_core.send_now(command)
    assert answer == DEFAULT_ANSWER

@pytest.mark.timeout(CNC_PROCESS_TIME * 5)
def test_send_now_while_paused(conn_core, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    conn_core.pause()
    time.sleep(CNC_PROCESS_TIME)
    command = "Random Command"
    answer = conn_core.send_now(command)
    assert answer == DEFAULT_ANSWER

@pytest.mark.timeout(CNC_PROCESS_TIME * 2)
def test_start_sets_working(conn_core, mocked_serial_port, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(0.1) # give time for the file to be processed
    assert conn_core.is_working()
    assert serial.Serial.write.call_count >= 0

@pytest.mark.timeout(CNC_PROCESS_TIME * 4)
def test_pause_while_working(conn_core, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    conn_core.pause()
    calls_at_pause = serial.Serial.write.call_count
    time.sleep(CNC_PROCESS_TIME)
    current_calls = serial.Serial.write.call_count
    new_calls = current_calls - calls_at_pause
    assert new_calls == 0

@pytest.mark.timeout(CNC_PROCESS_TIME * 5)
def test_resume(conn_core, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    conn_core.pause()
    time.sleep(CNC_PROCESS_TIME)
    calls_before_resume = serial.Serial.write.call_count
    conn_core.resume()
    time.sleep(CNC_PROCESS_TIME)
    current_calls = serial.Serial.write.call_count
    new_calls = current_calls - calls_before_resume
    assert new_calls > 0


""" 
Test cancel
"""
@pytest.mark.timeout(CNC_PROCESS_TIME * 4)
def test_cancel_while_working(conn_core, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    conn_core.cancel()
    time.sleep(CNC_PROCESS_TIME)
    assert not conn_core.is_working()
    assert threading.active_count() == 3

@pytest.mark.timeout(CNC_PROCESS_TIME * 5)
def test_cancel_while_paused(conn_core, gcode_file):
    conn_core.start(gcode_file)
    time.sleep(CNC_PROCESS_TIME)
    conn_core.pause()
    time.sleep(CNC_PROCESS_TIME)
    conn_core.cancel()
    time.sleep(CNC_PROCESS_TIME)
    assert not conn_core.is_working()
    assert threading.active_count() == 3
