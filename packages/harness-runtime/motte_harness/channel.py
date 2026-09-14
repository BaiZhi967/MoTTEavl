class HarnessChannel:
    def send(self, message):
        raise NotImplementedError


class TerminalChannel(HarnessChannel):
    def send(self, message):
        raise PermissionError("terminal channel requires approval")
