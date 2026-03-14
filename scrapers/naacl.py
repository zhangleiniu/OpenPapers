from .acl_anthology import ACLAnthologyScraper


class NAACLScraper(ACLAnthologyScraper):
    NAME = "NAACL"

    def __init__(self):
        super().__init__('naacl')
