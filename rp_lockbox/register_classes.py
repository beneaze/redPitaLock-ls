from labscript_devices import register_classes

register_classes(
    'rp_lockbox',
    BLACS_tab='user_devices.rp_lockbox.blacs_tabs.RPLockboxTab',
    runviewer_parser=None,
)
