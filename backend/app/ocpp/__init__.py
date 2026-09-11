"""OCPP 1.6J layer.

- ``app.ocpp.csms``: the CSMS WebSocket server (Central System) the simulated charge points
  connect to. It runs IN-PROCESS with FastAPI: the app lifespan calls ``start_csms()`` at startup
  and ``stop_csms(server)`` at shutdown.
- ``app.ocpp.handlers``: the inbound message handlers and the outbound calls
  (``CentralSystemChargePoint``).
- ``app.ocpp.registry``: in-memory state shared by the CSMS, the handlers and the API
  (connected charge points, pending plug-ins, manual limits, the raw OCPP frame log).

This package module deliberately imports nothing, so importing ``app.ocpp.registry`` never
pulls in the server or the handlers and cannot create an import cycle.
"""
