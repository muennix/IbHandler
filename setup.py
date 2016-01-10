#!/usr/bin/env python

from setuptools import setup
setup(name='mxIbHandler',
      version='0.91',
      py_modules=['IbHandler'],
      description='A simple handler for the Interactive Brokers API',
      author='Michael Muennix',
      author_email='michael@muennix.com',
      install_requires=['IbPy'],
      dependency_links=['https://github.com/muennix/IbPy/tarball/master#egg=IbPy-0.7.7'],
      )