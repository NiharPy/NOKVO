import asyncio
from app.services.re_agent_scheduler import REAgentScheduler

async def test():
    note = "Customer Rajesh wants to visit Skyline project tomorrow at 2 PM. Phone: 9876543210"
    res = await REAgentScheduler._extract(note)
    print("Extracted:", res)

asyncio.run(test())
