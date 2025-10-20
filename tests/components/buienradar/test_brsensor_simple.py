from datetime import datetime
from unittest.mock import MagicMock

from homeassistant.components.buienradar.sensor import BrSensor
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    SENSOR_COMAPASS_OUTLINE,
    SENSOR_MDI_GAUGE,
    SENSOR_WEATHER_WINDY,
    WEATHER_PARTLY_CLOUDY_OUTLINE,
    WEATHER_POURING_OUTLINE,
)

# Import your BrSensor class
# from your_module import BrSensor

# Local fallback constants to avoid importing buienradar.constants
FORECAST = "forecast"
CONDITION = "condition"
IMAGE = "image"

ATTR_ATTRIBUTION = "attribution"
ATTRIBUTION = "Buienradar"
TIMEFRAME = "Timeframe"


class TestConstantsAreImportable:
    """Test that constants can be imported and used."""

    def test_all_icon_constants_are_strings(self):
        """Test that all icon constants are valid strings."""
        icons = [
            SENSOR_MDI_GAUGE,
            SENSOR_WEATHER_WINDY,
            SENSOR_COMAPASS_OUTLINE,
            WEATHER_POURING_OUTLINE,
            WEATHER_PARTLY_CLOUDY_OUTLINE,
        ]

        for icon in icons:
            assert isinstance(icon, str)
            assert icon.startswith("mdi:")

    def test_icon_constants_follow_naming_convention(self):
        """Test that icon constants follow MDI naming convention."""
        icons = {
            "gauge": SENSOR_MDI_GAUGE,
            "weather-windy": SENSOR_WEATHER_WINDY,
            "compass-outline": SENSOR_COMAPASS_OUTLINE,
            "weather-pouring": WEATHER_POURING_OUTLINE,
            "weather-partly-cloudy": WEATHER_PARTLY_CLOUDY_OUTLINE,
        }

        for expected_suffix, icon_constant in icons.items():
            assert icon_constant == f"mdi:{expected_suffix}"


class TestBrSensorHelpers:
    """Minimal test suite for BrSensor helper methods."""

    def setup_method(self):
        """Set up test sensor before each test."""
        description = SensorEntityDescription(key="test")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)

    def test_get_forecast_day_returns_correct_index(self):
        """Test that forecast day extraction works."""
        # Day 1 should return 0, Day 2 should return 1, etc.
        assert self.sensor._get_forecast_day("temperature_1d") == 0
        assert self.sensor._get_forecast_day("temperature_2d") == 1
        assert self.sensor._get_forecast_day("temperature_5d") == 4

    def test_windspeed_converts_to_kmh(self):
        """Test windspeed conversion from m/s to km/h."""
        data = {"windspeed": 10.0}  # 10 m/s
        self.sensor._attr_native_value = 10.0

        result = self.sensor._load_windspeed_data(data, "windspeed")

        assert result is True
        assert self.sensor._attr_native_value == 36.0  # 10 * 3.6 = 36 km/h

    def test_load_default_data_returns_false_for_none(self):
        """Test that load_default_data returns False when value is None."""
        data = {"attribution": "Buienradar", "stationname": "De Bilt"}

        result = self.sensor._load_default_data(data, "nonexistent")

        assert result is False

    def test_load_data_skips_update_when_unchanged(self):
        """Test that _load_data returns False when measurement hasn't changed."""
        from datetime import datetime

        measured_time = datetime(2025, 10, 19, 12, 0, 0)
        data = {"measured": measured_time, "temperature": 20.0}

        # Set measured time to same value
        self.sensor._measured = measured_time

        result = self.sensor._load_data(data)

        assert result is False


class TestLoadVisibilityData:
    """Tests for _load_visibility_data method."""

    def setup_method(self):
        """Set up test sensor."""
        description = SensorEntityDescription(key="visibility")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)

    def test_visibility_converts_meters_to_km(self):
        """Test visibility conversion from meters to km."""
        data = {"visibility": 10000}
        self.sensor._attr_native_value = 10000

        result = self.sensor._load_visibility_data(data, "visibility")

        assert result is True
        assert self.sensor._attr_native_value == 10.0

    def test_visibility_with_none_state(self):
        """Test visibility when state is None."""
        data = {"visibility": 15000}
        self.sensor._attr_native_value = None

        result = self.sensor._load_visibility_data(data, "visibility")

        assert result is True
        assert self.sensor._attr_native_value == 15000 / 1000

    def test_visibility_rounds_correctly(self):
        """Test visibility rounding to 1 decimal place."""
        data = {"visibility": 12500}
        self.sensor._attr_native_value = 12500

        result = self.sensor._load_visibility_data(data, "visibility")

        assert result is True
        assert self.sensor._attr_native_value == 12.5


class TestLoadConditionData:
    """Tests for _load_condition_data method."""

    def setup_method(self):
        """Set up test sensor."""
        description = SensorEntityDescription(key="condition")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)

    def test_load_condition_data_updates_state(self):
        """Test loading condition data updates state and picture."""
        data = {
            "condition": {
                "condition": "rainy",
                "image": "https://example.com/rain.png",
            }
        }
        self.sensor._attr_native_value = None
        self.sensor._attr_entity_picture = None

        result = self.sensor._load_condition_data(data, "condition")

        assert result is True
        assert self.sensor._attr_native_value == "rainy"
        assert self.sensor._attr_entity_picture == "https://example.com/rain.png"

    def test_load_condition_data_no_change(self):
        """Test that no update occurs when condition hasn't changed."""
        data = {
            "condition": {
                "condition": "rainy",
                "image": "https://example.com/rain.png",
            }
        }
        self.sensor._attr_native_value = "rainy"
        self.sensor._attr_entity_picture = "https://example.com/rain.png"

        result = self.sensor._load_condition_data(data, "condition")

        assert result is False

    def test_load_condition_data_no_condition(self):
        """Test when condition data is missing."""
        data = {"condition": None}

        result = self.sensor._load_condition_data(data, "condition")

        assert result is False


class TestLoadPrecipitationForecastData:
    """Tests for _load_precipitation_forecast_data method."""

    def setup_method(self):
        """Set up test sensor."""
        description = SensorEntityDescription(key="precipitation_forecast_average")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)

    def test_load_precipitation_forecast_average(self):
        """Test loading precipitation forecast average."""
        data = {
            "precipitation_forecast": {
                "timeframe": 60,
                "average": 2.5,
            }
        }

        result = self.sensor._load_precipitation_forecast_data(
            data, "precipitation_forecast_average"
        )

        assert result is True
        assert self.sensor._timeframe == 60
        assert self.sensor._attr_native_value == 2.5

    def test_load_precipitation_forecast_total(self):
        """Test loading precipitation forecast total."""
        data = {
            "precipitation_forecast": {
                "timeframe": 120,
                "total": 10.0,
            }
        }

        result = self.sensor._load_precipitation_forecast_data(
            data, "precipitation_forecast_total"
        )

        assert result is True
        assert self.sensor._timeframe == 120
        assert self.sensor._attr_native_value == 10.0


class TestLoadDefaultData:
    """Tests for _load_default_data method."""

    def setup_method(self):
        """Set up test sensor."""
        description = SensorEntityDescription(key="temperature")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)
        self.sensor._measured = None

    def test_load_default_data_success(self):
        """Test loading default data successfully."""
        data = {
            "temperature": 20.5,
            "attribution": "Buienradar",
            "stationname": "De Bilt",
        }

        result = self.sensor._load_default_data(data, "temperature")

        assert result is True
        assert self.sensor._attr_native_value == 20.5

    def test_load_default_data_none_value(self):
        """Test that method returns False when value is None."""
        data = {
            "attribution": "Buienradar",
            "stationname": "De Bilt",
        }

        result = self.sensor._load_default_data(data, "nonexistent")

        assert result is False

    def test_load_default_data_sets_attributes(self):
        """Test that attributes are set correctly."""
        data = {
            "humidity": 75,
            "attribution": "Buienradar",
            "stationname": "De Bilt",
        }

        result = self.sensor._load_default_data(data, "humidity")

        assert result is True
        assert self.sensor._attr_extra_state_attributes["attribution"] == "Buienradar"
        assert self.sensor._attr_extra_state_attributes["Stationname"] == "De Bilt"


class TestLoadForecastData:
    """Tests for _load_forecast_data method."""

    def setup_method(self):
        """Set up test sensor."""
        description = SensorEntityDescription(key="temperature_1d")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)

    def test_load_forecast_temperature(self):
        """Test loading forecast temperature."""
        data = {
            "forecast": [
                {"temperature": 18.0},
                {"temperature": 20.0},
            ]
        }

        result = self.sensor._load_forecast_data(data, "temperature_1d")

        assert result is True
        assert self.sensor._attr_native_value == 18.0

    def test_load_forecast_day_2(self):
        """Test loading forecast for day 2."""
        data = {
            "forecast": [
                {"temperature": 18.0},
                {"temperature": 20.0},
            ]
        }

        result = self.sensor._load_forecast_data(data, "temperature_2d")

        assert result is True
        assert self.sensor._attr_native_value == 20.0

    def test_load_forecast_missing_day(self):
        """Test handling missing forecast day."""
        data = {
            "forecast": [
                {"temperature": 18.0},
            ]
        }

        result = self.sensor._load_forecast_data(data, "temperature_5d")

        assert result is False


class TestLoadData:
    """Integration tests for _load_data method."""

    def setup_method(self):
        """Set up test sensor."""
        description = SensorEntityDescription(key="temperature")
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        self.sensor = BrSensor("Test", coordinates, description)

    def test_load_data_skips_update_when_unchanged(self):
        """Test that _load_data returns False when measurement hasn't changed."""
        measured_time = datetime(2025, 10, 19, 12, 0, 0)
        data = {
            "measured": measured_time,
            "temperature": 20.0,
        }

        self.sensor._measured = measured_time

        result = self.sensor._load_data(data)

        assert result is False

    def test_load_data_updates_measured_time(self):
        """Test that measured time is updated."""
        measured_time = datetime(2025, 10, 19, 12, 0, 0)
        data = {
            "measured": measured_time,
            "temperature": 20.0,
            "attribution": "Buienradar",
            "stationname": "De Bilt",
        }

        self.sensor._measured = None

        result = self.sensor._load_data(data)

        assert result is True
        assert self.sensor._measured == measured_time

    def test_load_data_windspeed_sensor(self):
        """Test loading windspeed sensor."""
        self.sensor.entity_description = SensorEntityDescription(key="windspeed")
        self.sensor._attr_native_value = 10.0

        data = {
            "measured": datetime(2025, 10, 19, 12, 0, 0),
            "windspeed": 10.0,
        }

        result = self.sensor._load_data(data)

        assert result is True
        assert self.sensor._attr_native_value == 36.0


class TestLoadForecastConditionData:
    """Tests for _load_forecast_condition_data method."""

    def setup_method(self):
        """Prepare a test BrSensor instance."""
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        description = SensorEntityDescription(key="test")
        self.sensor = BrSensor("Test", coordinates, description)
        self.sensor._attr_native_value = "new"
        self.sensor._attr_entity_picture = "old.png"
        self.sensor._get_condition_state = MagicMock(return_value="new")

    def test_forecast_index_out_of_range(self, caplog):
        """Should return False and log a warning when index is out of range."""
        data = {FORECAST: []}

        result = self.sensor._load_forecast_condition_data(data, "symbol", 2)

        assert result is False
        assert any("No forecast" in msg for msg in caplog.messages)

    def test_forecast_condition_none(self):
        """Should return False when condition is missing or None."""
        data = {FORECAST: [{CONDITION: None}]}

        result = self.sensor._load_forecast_condition_data(data, "symbol", 0)
        assert result is False

    def test_forecast_condition_new_state(self):
        """Should update state and picture when they change."""
        data = {
            FORECAST: [
                {
                    CONDITION: {
                        IMAGE: "new.png",
                        "condition": "cloudy",
                    }
                }
            ]
        }

        result = self.sensor._load_forecast_condition_data(data, "symbol", 0)
        assert result is True
        assert self.sensor._attr_native_value == "new"
        assert self.sensor._attr_entity_picture == "new.png"

    def test_forecast_condition_same_state(self):
        """Should return False when nothing changes."""
        self.sensor._attr_native_value = "new"
        self.sensor._attr_entity_picture = "same.png"
        self.sensor._get_condition_state.return_value = "new"

        data = {
            FORECAST: [
                {
                    CONDITION: {
                        IMAGE: "same.png",
                        "condition": "cloudy",
                    }
                }
            ]
        }

        result = self.sensor._load_forecast_condition_data(data, "symbol", 0)
        assert result is False

    # 5️⃣ Condition same state and same image → no update
    def test_same_state_and_image(self):
        self.sensor._attr_native_value = "sunny"
        self.sensor._attr_entity_picture = "same.png"
        self.sensor._get_condition_state.return_value = "sunny"

        data = {
            FORECAST: [
                {
                    CONDITION: {
                        IMAGE: "same.png",
                        "condition": "clear",
                    }
                }
            ]
        }

        result = self.sensor._load_forecast_condition_data(data, "symbol", 0)
        assert result is False


class TestLoadForecastWindspeedData:
    """Full branch coverage for _load_forecast_windspeed_data."""

    def setup_method(self):
        coordinates = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        desc = SensorEntityDescription(key="test")
        self.sensor = BrSensor("Test", coordinates, desc)

    # 1️⃣ IndexError branch → missing forecast
    def test_index_out_of_range(self, caplog):
        data = {FORECAST: []}
        result = self.sensor._load_forecast_windspeed_data(data, "windspeed_1", 5)
        assert result is False
        assert any("forecast" in msg.lower() for msg in caplog.messages)

    # 3️⃣ Has state (previously set) → conversion to km/h (state * 3.6)
    def test_load_value_with_state_conversion(self):
        data = {FORECAST: [{"windspeed": 10.0}]}
        self.sensor._attr_native_value = None
        self.sensor._attr_native_value = 10.0
        # simulate previous state value in m/s (used for conversion)
        self.sensor._attr_native_value = 10.0
        self.sensor._attr_native_value = None  # reset before call
        # simulate self.state by temporarily faking property
        type(self.sensor).state = property(lambda s: 5.0)

        result = self.sensor._load_forecast_windspeed_data(data, "windspeed_1", 0)

        # Clean up patched property
        del type(self.sensor).state

        assert result is True
        # 5.0 m/s * 3.6 = 18.0 km/h
        assert self.sensor._attr_native_value == 18


class TestSetPrecipitationAttributes:
    """Covers _set_precipitation_attributes completely."""

    def setup_method(self):
        coords = {CONF_LATITUDE: 52.0, CONF_LONGITUDE: 5.0}
        desc = SensorEntityDescription(key="precipitation")
        self.sensor = BrSensor("Test", coords, desc)

    def test_without_timeframe(self):
        """Should only include attribution when timeframe is None."""
        self.sensor._timeframe = None
        data = {"attribution": "Buienradar"}

        self.sensor._set_precipitation_attributes(data)

        assert self.sensor._attr_extra_state_attributes == {
            ATTR_ATTRIBUTION: ATTRIBUTION
        }

    def test_with_timeframe(self):
        """Should include timeframe label when available."""
        self.sensor._timeframe = 10
        data = {"attribution": "Buienradar"}

        self.sensor._set_precipitation_attributes(data)

        assert self.sensor._attr_extra_state_attributes == {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            TIMEFRAME: "10 min",
        }
