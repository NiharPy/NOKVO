class PlivoService:
    @staticmethod
    async def provision_plivo(tenant_id: str) -> dict:
        """
        Creates a Plivo provisioning abstraction.
        """
        return {
            "plivo_subaccount_id": None,
            "phone_number_status": "pending"
        }
