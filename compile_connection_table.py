"""Compile the connection table to an HDF5 file for BLACS."""
import sys
sys.argv = [__file__, r'C:\Experiments\rp_lockbox\connection_table.h5']

from labscript import labscript_init, start, stop

labscript_init(r'C:\Experiments\rp_lockbox\connection_table.h5', new=True)

from user_devices.rp_lockbox.labscript_devices import RPLockbox

RPLockbox('rp_lockbox', ip_addr='10.0.0.15')

start()
stop(1)

print('Connection table compiled to C:\\Experiments\\rp_lockbox\\connection_table.h5')
