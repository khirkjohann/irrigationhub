import sys


if __name__ == '__main__':
    print('Mock data generator is disabled for hardware-only mode.')
    print('Use app.py sensor polling with real BME280 + ADS1115 inputs.')
    sys.exit(1)