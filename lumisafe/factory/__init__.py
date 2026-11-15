"""Bot factory: many paper bots (quant + qualitative), each with its own virtual book on real prices.

Every bot's decision is frozen with a timestamp before the open it trades at, so the record
can't be fitted after the fact. Books are virtual (no orders), priced with the same engine
as the strategy zoo: next-open execution, real spreads, 0.05% slippage. Nothing here
touches real money; the owner may copy any bot by hand.
"""
