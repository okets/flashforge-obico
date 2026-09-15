import copy

import pytest

# The captured `detail` reply of an idle Creator 5 Pro on firmware 1.9.9 (MAC redacted).
DETAIL = {
    "status": "ready", "printFileName": "", "printProgress": 0.0,
    "printDuration": 0, "estimatedTime": 0.0, "printLayer": 0, "targetPrintLayer": 0,
    "errorCode": "", "doorStatus": "close", "lightStatus": "close",
    "model": "Creator 5 Pro", "name": "Creator 5 Pro", "firmwareVersion": "1.9.9",
    "ipAddr": "10.0.0.10", "macAddr": "00:00:00:00:00:00", "location": "Den",
    "measure": "256X256X256", "nozzleCnt": 4, "nozzleModel": "0.4mm;0.4mm;0.4mm;0.4mm",
    "nozzleStyle": 0, "nozzleTemps": [28, 29, 29, 29], "nozzleTargetTemps": [0, 0, 0, 0],
    "platTemp": 27, "platTargetTemp": 0, "chamberTemp": 27, "chamberTargetTemp": 0,
    "camera": 1, "cameraStreamUrl": "http://10.0.0.10:8080/?action=stream",
    "lidar": 1, "pid": 41,
    "matlStationInfo": {"currentLoadSlot": 0, "currentSlot": 0, "slotCnt": 4,
                        "stateAction": 0, "stateStep": 0, "slotInfos": []},
}


@pytest.fixture
def detail():
    return copy.deepcopy(DETAIL)
