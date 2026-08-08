import influxdb_client, os, time
import tinytuya
import json
import logging

try:
    import yaml
except ImportError:
    print("PyYAML is not installed. Please install it using 'pip install pyyaml'")
    exit(1)

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

def load_config():
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
        with open(config_path, 'r') as file:
            return yaml.safe_load(file)
    except FileNotFoundError:
        print("Config file 'config.yaml' not found. Please ensure it exists in the same directory as the script.")
        exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing config.yaml: {e}")
        exit(1)
    except Exception as e:
        print(f"Error loading config file: {e}")
        exit(1)

# Load configuration
config = load_config()

# Logging configuration
logging.basicConfig(
    level=getattr(logging, config['logging']['level']),
    format=config['logging']['format'],
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config['logging']['file'])
    ]
)
logger = logging.getLogger(__name__)

# Initialize InfluxDB client
try:
    write_client = influxdb_client.InfluxDBClient(
        url=config['influxdb']['url'],
        token=config['influxdb']['token'],
        org=config['influxdb']['org']
    )
    write_api = write_client.write_api(write_options=SYNCHRONOUS)
    logger.info("Successfully initialized InfluxDB client")
except Exception as e:
    logger.error(f"Failed to initialize InfluxDB client: {e}")
    exit(1)

# Initialize Tuya device
d = tinytuya.Device(
    config['tuya']['device_id'],
    config['tuya']['ip_address'],
    config['tuya']['local_key'],
    version=config['tuya']['version']
)

# Define DPS type mappings
DPS_TYPES = {
    # Numeric values
    '2': 'numeric',  # temperature
    '3': 'numeric',  # humidity
    '4': 'numeric',  # pm2.5
    '5': 'numeric',  # pm10
    '7': 'numeric',
    '8': 'numeric',
    '9': 'numeric',
    '22': 'numeric',
    '101': 'numeric',
    '102': 'numeric',
    '104': 'numeric',
    '105': 'numeric',
    '107': 'numeric',
    '109': 'numeric',
    '110': 'numeric',
    '111': 'numeric',
    '113': 'numeric',
    '114': 'numeric',
    '115': 'numeric',
    # String/Enum values
    '1': 'enum',   # level
    '28': 'enum',  # mode
    '112': 'string', # status
    # Boolean values
    '23': 'boolean',
    '106': 'boolean',
    '116': 'boolean',
    '117': 'boolean',
    '118': 'boolean'
}

def write_to_influxdb(write_api, bucket, org, device_id, dps_key, value, data_type):
    """Helper function to write data points to InfluxDB"""
    try:
        measurement = f"tuya_device_{data_type}"
        
        # Convert value based on data type
        if data_type == 'numeric':
            value = float(value)
        elif data_type == 'boolean':
            value = bool(value)
        else:  # string
            value = str(value)

        point = (
            Point(measurement)
            .tag("device_id", device_id)
            .tag("dps_key", dps_key)
            .field("value", value)
        )
        
        write_api.write(bucket=bucket, org=org, record=point)
        logger.debug(f"Successfully wrote {data_type} data point for key {dps_key}: {value}")
        
    except Exception as e:
        logger.error(f"Failed to write {data_type} data point for key {dps_key}: {e}")

while True:
    try:
        # Get device status
        data = d.status()
        logger.debug(f"Received device status: {data}")
        
        # Extract DPS values and write to InfluxDB
        if 'dps' in data:
            for key, value in data['dps'].items():
                try:
                    # Determine data type from mapping
                    data_type = DPS_TYPES.get(key, 'string')  # default to string if type not specified
                    
                    write_to_influxdb(
                        write_api,
                        config['influxdb']['bucket'],
                        config['influxdb']['org'],
                        config['tuya']['device_id'],
                        key,
                        value,
                        data_type
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to process data point for key {key}: {e}")
        else:
            logger.warning("No 'dps' data found in device status")
            
    except Exception as e:
        logger.error(f"Error communicating with Tuya device: {e}")
    
    try:
        time.sleep(config['general']['polling_interval'])
    except KeyboardInterrupt:
        logger.info("Script terminated by user")
        write_client.close()
        exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")