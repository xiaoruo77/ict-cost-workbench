import unittest
from decimal import Decimal
from cost_core.declarative import compile_input, contract, configured_metrics
from cost_core.industry_catalog import catalog_payload
from cost_core.costing import CostingEngine

class DeclarativeTest(unittest.TestCase):
    def rows(self):
        return [dict(object_id=k,object_name=k,quantity=5,selling_price=20,module="staff",
                     amount=n,service_hours=h,occupied_area=10-h) for k,n,h in [("A",40,2),("B",60,4)]]

    def payload(self):
        return dict(version="1.0.0",industry_code="8053",period="2026-11",shared_pools={"space":30,"support":30},rows=self.rows())

    def test_catalog(self):
        d=catalog_payload()
        self.assertEqual(d["counts"],[20,97,473,1382])
        self.assertEqual(len(d["industries"]),1972)
        codes={row["code"] for row in d["industries"]}
        self.assertTrue({"277","278","2770","2780","8052","8053"} <= codes)

    def test_new_profile(self):
        schema=contract("8053")
        self.assertEqual(schema["coverage"],"configured-template")
        self.assertEqual(schema["custom_fields"][0]["id"],"service_hours")
        self.assertEqual(contract("0111")["coverage"],"starter-template")
        result=CostingEngine().analyze(compile_input(self.payload()))
        for k,v in {"total_cost":160,"total_revenue":200,"total_operating_margin":40,"unallocated_cost":0}.items():
            self.assertEqual(Decimal(result["totals"][k]),v)
        self.assertEqual(len(compile_input(self.payload()).pools),2)
        self.assertEqual(configured_metrics(compile_input(self.payload()),schema)["hours_total"]["value"],"6")
        self.assertTrue(result["allocations"])
        self.assertTrue(result["report"])

    def test_validation(self):
        for patch in [dict(version="9"),dict(period="2026-13"),dict(shared_amount=-1),
                      dict(industry_code="bogus"),dict(extra="x")]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                compile_input({**self.payload(),**patch})
        for field,value in [("service_hours","NaN"),("amount","1e99999"),("module","unknown")]:
            p=self.payload()
            p["rows"][0][field]=value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compile_input(p)
