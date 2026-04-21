from setuptools import setup, find_packages

setup(
    name='rp_lockbox',
    version='0.1.0',
    packages=find_packages(),
    python_requires='>=3.9,<3.10',
    install_requires=[
        'labscript-suite>=3.3.0',
        'PyQt5>=5.15',
        'numpy>=1.21,<2',
        'scipy>=1.7',
        'pyqtgraph>=0.12',
        'h5py>=2.9',
    ],
)
