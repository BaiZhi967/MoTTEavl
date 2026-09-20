Sleep far longer than the agent timeout so the trial raises a real Harbor
agent-timeout exception. Used to capture the native failure payload once; the
parser then replays the frozen bytes offline.
