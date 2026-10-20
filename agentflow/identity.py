"""Stable visual identity, independent of profession or model."""
FACES = ("smile", "wink", "happy", "curious", "calm", "star", "cat", "bear", "robot", "dream", "sunny", "cheer")
COLORS = ("purple", "blue", "pink", "orange", "green", "teal", "indigo", "slate", "rose", "amber", "cyan", "mint")


def face_for(agent_id):
    return "smile" if agent_id == "a-lead" else FACES[sum(map(ord, agent_id)) % len(FACES)]
