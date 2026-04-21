from labscript import start, stop
from user_devices.rp_lockbox.labscript_devices import RPLockbox

# Change the IP address to match your Red Pitaya
RPLockbox('rp_lockbox', ip_addr='192.168.1.100')

if __name__ == '__main__':
    start()
    stop(1)
