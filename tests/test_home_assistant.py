import unittest
from unittest.mock import patch
from plugins import home_assistant as ha


class HomeAssistantTests(unittest.TestCase):
    def test_actual_temperature_units_converted_and_invalid_skipped(self):
        states = [
            {'entity_id':'sensor.room', 'state':'77', 'attributes':{'device_class':'temperature','unit_of_measurement':'°F'}},
            {'entity_id':'sensor.garden', 'state':'18.5','attributes':{'unit_of_measurement':'°C'}},
            {'entity_id':'sensor.temp_humidity', 'state':'50','attributes':{'unit_of_measurement':'%'}},
            {'entity_id':'sensor.temp_bad', 'state':'unavailable','attributes':{'unit_of_measurement':'°C'}},
            {'entity_id':'sensor.temp_nan', 'state':'nan','attributes':{'unit_of_measurement':'°C'}},
        ]
        with patch.object(ha, 'get_states', return_value=states):
            result = ha.run({'action':'temperature'})
        self.assertIn('sensor.room: 25 °C', result)
        self.assertIn('sensor.garden: 18.5 °C', result)
        self.assertNotIn('humidity', result)
        self.assertNotIn('bad', result)
        self.assertNotIn('nan', result)

    def test_query_also_converts_temperature(self):
        with patch.object(ha, 'get_state', return_value={'entity_id':'sensor.room','state':'68','attributes':{'unit_of_measurement':'°F'}}):
            result = ha.run({'action':'query','entity':'sensor.room'})
        self.assertIn('20 °C', result)
        self.assertNotIn('°F', result)

    def test_invalid_set_value_does_not_turn_device_off(self):
        with patch.object(ha, 'call_service') as call:
            result = ha.run({'action':'set','entity':'light.room','value':'banana'})
        call.assert_not_called()
        self.assertIn('on/off', result)

    def test_headless_writes_always_denied(self):
        with patch.object(ha, 'call_service') as call:
            result = ha.run({'action':'set','entity':'light.room','value':'on'})
        call.assert_not_called()
        self.assertIn('read-only', result)
