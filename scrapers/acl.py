from .acl_anthology import ACLAnthologyScraper


class ACLScraper(ACLAnthologyScraper):
    NAME = "ACL"

    def __init__(self):
        super().__init__('acl')
