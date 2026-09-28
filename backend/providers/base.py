from abc import ABC, abstractmethod


class ProviderUnavailable(Exception):
    """A provider cannot serve a request for a structural or
    configuration reason (unconfigured, no model selected).

    Defined once here, shared by every provider adapter and
    the model router. Messages are constant diagnostic text —
    never credential material.
    """

    def __init__(self, message: str, provider: str = ""):
        super().__init__(message)
        self.provider = provider




class AIProvider(ABC):

    @abstractmethod
    async def generate(self, messages, model=None, **kwargs):
        """
        Generate a response from the AI provider.
        """
        pass

    @abstractmethod
    async def health_check(self):
        """
        Check whether the provider is available.
        """
        pass

