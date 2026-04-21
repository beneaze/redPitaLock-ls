import numpy as np
from labscript import Device
from labscript.labscript import set_passed_properties


class RPLockbox(Device):
    """Labscript device for a Red Pitaya dual-channel PID lockbox via PyRPL.

    Provides two independent PID channels (pid0 on in1/out1, pid1 on in2/out2)
    with support for wwlyn's extended features: hold-with-preserved-output,
    setpoint sequences, and extended Ki bandwidth.

    Static per-shot parameters are written to the HDF5 file and applied by the
    BLACS worker during transition_to_buffered.
    """

    description = 'Red Pitaya Dual-Channel PID Lockbox (PyRPL)'
    allowed_children = []

    @set_passed_properties({'connection_table_properties': ['ip_addr']})
    def __init__(self, name, ip_addr, parent_device=None, **kwargs):
        Device.__init__(self, name, parent_device, connection=None, **kwargs)
        self.BLACS_connection = ip_addr
        self._ch_params = {0: {}, 1: {}}

    def set_pid_params(self, channel, **kwargs):
        """Set per-shot PID parameters for a channel.

        Valid keys: setpoint, p, i, min_voltage, max_voltage, pause_gains,
        inputfilter (list of 4 floats).
        """
        if channel not in (0, 1):
            raise ValueError(f"channel must be 0 or 1, got {channel}")
        self._ch_params[channel].update(kwargs)

    def set_setpoint_sequence(self, channel, array):
        """Load a setpoint sequence (up to 16 values) for DIO-triggered stepping."""
        if channel not in (0, 1):
            raise ValueError(f"channel must be 0 or 1, got {channel}")
        array = list(array)
        if len(array) > 16:
            array = array[:16]
        self._ch_params[channel]['setpoint_sequence'] = array

    def generate_code(self, hdf5_file):
        Device.generate_code(self, hdf5_file)
        grp = hdf5_file.require_group(f'/devices/{self.name}/')
        for ch in (0, 1):
            params = self._ch_params[ch]
            if not params:
                continue
            ch_grp = grp.require_group(f'ch{ch}')
            for key, value in params.items():
                if isinstance(value, (list, tuple)):
                    arr = np.array(value, dtype=float)
                    ch_grp.create_dataset(key, data=arr)
                elif isinstance(value, str):
                    ch_grp.create_dataset(
                        key, data=np.bytes_(value.encode('utf-8'))
                    )
                elif isinstance(value, bool):
                    ch_grp.create_dataset(key, data=bool(value))
                else:
                    ch_grp.create_dataset(key, data=float(value))
