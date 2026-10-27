"""Only development-validated complete adapters are published here."""

from chaos.baselines.arm import B3Arm
from chaos.baselines.naive_arm import B0Arm
from chaos.baselines.queue_arm import B1Arm, B2Arm

ARMS={'b0':B0Arm,'b1':B1Arm,'b2':B2Arm,'b3':B3Arm}
