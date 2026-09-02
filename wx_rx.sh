#!/bin/bash
# NWR SAME watch v2: same_bridge.py orchestrates rtl_fm + multimon-ng + voice lane
[ -f /opt/wx-sdr/wx.env ] && set -a && . /opt/wx-sdr/wx.env && set +a
exec /opt/wx-sdr/venv/bin/python -u /opt/wx-sdr/same_bridge.py
