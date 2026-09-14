class ProviderError(Exception): pass
class ProviderHTTPError(ProviderError): pass
class ProviderProtocolError(ProviderError): pass
class ProviderAuthenticationError(ProviderError): pass
