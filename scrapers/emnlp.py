from .acl_anthology import ACLAnthologyScraper


class EMNLPScraper(ACLAnthologyScraper):
    NAME = "EMNLP"

    def __init__(self):
        super().__init__('emnlp')
