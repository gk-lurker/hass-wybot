"""Constants for the WyBot integration."""
DOMAIN = "gk_hass_wybot"
TIMEOUT = 30
MANUFACTURER = "WyBot"

# Config / Options keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

OPT_DP0_DELAY_SECONDS = "dp0_delay_seconds"
OPT_TS_OFFSET_SECONDS = "ts_offset_seconds"

# Defaults
DEFAULT_DP0_DELAY_SECONDS = 6.0
DEFAULT_TS_OFFSET_SECONDS = 0

# Clean-time options (used by WY460; app supports discrete 1h/2h/3h/4h)
CONF_CLEAN_TIME_MINUTES = "clean_time_minutes"
CONF_USE_CLEAN_TIME = "use_clean_time"

# WY460 observed values:
# 1h  =  60 -> 3c000000
# 2h  = 120 -> 78000000
# 3h  = 180 -> b4000000
# 4h  = 240 -> f0000000
CLEAN_TIME_ALLOWED_MINUTES = (60, 120, 180, 240)

DEFAULT_CLEAN_TIME_MINUTES = 60
CLEAN_TIME_MIN = 60
CLEAN_TIME_MAX = 240
CLEAN_TIME_STEP = 60
