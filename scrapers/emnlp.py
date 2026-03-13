from .acl_anthology import ACLAnthologyScraper


class EMNLPScraper(ACLAnthologyScraper):
    """EMNLP conference scraper (aclanthology.org)."""

    def __init__(self):
        super().__init__('emnlp')
