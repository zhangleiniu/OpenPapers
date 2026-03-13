from .acl_anthology import ACLAnthologyScraper


class NAACLScraper(ACLAnthologyScraper):
    """NAACL conference scraper (aclanthology.org)."""

    def __init__(self):
        super().__init__('naacl')
