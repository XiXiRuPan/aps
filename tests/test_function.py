#!/usr/bin/env python

# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import pytest
import importlib

from aps.libs import dynamic_importlib, ApsRegisters
from aps.conf import load_dict


@pytest.mark.parametrize(
    "str_lib",
    ["data/external/nnet.py:VoiceFilter", "data/external/task.py:DpclTask"])
def test_import_lib(str_lib):
    dynamic_importlib(str_lib)


@pytest.mark.parametrize("str_dict", ["data/dataloader/am/dict"])
def test_load_dict(str_dict):
    load_dict(str_dict)
    load_dict(str_dict, reverse=True)


def test_register():
    for package in ["asr", "sse", "task", "loader", "trainer", "transform"]:
        importlib.import_module(f"aps.{package}")
    for c in ApsRegisters.container:
        print(c)
