from .acl_anthology import ACLAnthologyScraper


class ACLScraper(ACLAnthologyScraper):
    """ACL conference scraper (aclanthology.org)."""

    def __init__(self):
        super().__init__('acl')
