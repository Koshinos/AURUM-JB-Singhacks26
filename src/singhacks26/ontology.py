"""Aurum property-graph ontology v1: explicit domains, ranges and identities.

This is a closed ingestion contract, not an OWL reasoner. Relationship direction
is part of the contract; subjective notes and curated themes are not facts inferred
by a language model. All identities are namespaced by class.
"""

VERSION = "aurum/1.0"
CLASSES = {
    "Client": "A client household in the supplied book.",
    "Portfolio": "An account owned by one client, including custody accounts.",
    "Position": "One instrument in one portfolio at one snapshot date.",
    "Instrument": "A security or product in the supplied instrument master.",
    "Mandate": "A named investment mandate; custody is not governed by it.",
    "AllocationBand": "An asset-class constraint belonging to a mandate.",
    "CashNeed": "A recorded future cash requirement, retaining certainty and currency.",
    "Commitment": "A recorded fund commitment, retaining called and uncalled amounts.",
    "CreditFacility": "A credit facility with dated collateral and borrowing fields.",
    "Transaction": "A recorded trade, cash flow or facility transaction.",
    "Event": "An authoritative event from event_log.csv, not live news.",
    "MarketObservation": "A market series value at a particular snapshot.",
    "Note": "An attributed RM observation, not a verified client belief.",
    "Theme": "An existing curated instrument grouping, not proven causation.",
}

# relationship: (domain, range)
RELATIONSHIPS = {
    "OWNS": ("Client", "Portfolio"),
    "HAS_POSITION": ("Portfolio", "Position"),
    "OF_INSTRUMENT": ("Position", "Instrument"),
    "GOVERNED_BY": ("Portfolio", "Mandate"),
    "HAS_BAND": ("Mandate", "AllocationBand"),
    "HAS_CASH_NEED": ("Client", "CashNeed"),
    "HAS_COMMITMENT": ("Client", "Commitment"),
    "COMMITTED_IN": ("Commitment", "Portfolio"),
    "HAS_FACILITY": ("Client", "CreditFacility"),
    "SECURED_BY": ("CreditFacility", "Portfolio"),
    "HAS_TRANSACTION": ("Portfolio", "Transaction"),
    "TRANSACTS_IN": ("Transaction", "Instrument"),
    "HAS_NOTE": ("Client", "Note"),
    "MAPPED_TO": ("Instrument", "Theme"),
}
