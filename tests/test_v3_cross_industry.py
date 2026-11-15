"""Verify three distinct economic activities use one compiled contract."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json, unittest
from unittest.mock import patch
from cost_core.declarative import compile_input, configured_metrics, contract
from cost_core.costing import CostingEngine

class CrossIndustry(unittest.TestCase):
    def test_different_profiles_only(self):
        cases = [
            ("0111","batch","kg","irrigation","water_hours"),
            ("2922","product","piece","mold","machine_hours"),
            ("8053","service","visit","space","occupied_area"),
            ("6513","project","hour","review","review_hours"),
        ]
        with TemporaryDirectory() as d, patch("cost_core.industry_profiles.ROOT",Path(d)):
            for code, kind, unit, pool, driver in cases:
                with self.subTest(code=code):
                    profile={"version":"1.0.0","industry_code":code,
                         "fields":[dict(id=driver,label=driver,type="decimal",required=True)],
                         "modules":{"direct":dict(label="direct",behavior="variable",cost_type="direct"),
                                    "shared":dict(label="shared",behavior="fixed",cost_type="indirect")},
                         "allocation":None,"pools":{pool:dict(driver=driver,module="shared")},
                         "metrics":{"scale":dict(field=driver,op="sum")}}
                    (Path(d)/(code+".json")).write_text(json.dumps(profile))
                    rows=[dict(object_id=k,object_name=k,object_kind=kind,quantity_unit=unit,
                               quantity=q,selling_price=10,module="direct",amount=cost,**{driver:weight})
                          for k,q,cost,weight in [("a",2,6,1),("b",4,14,3)]]
                    req=compile_input(dict(version="1.0.0",industry_code=code,period="2026-11",
                                           rows=rows,shared_pools={pool:20}))
                    r=CostingEngine().analyze(req)
                    self.assertEqual(Decimal(r["totals"]["total_cost"]),40)
                    self.assertEqual(Decimal(r["totals"]["total_operating_margin"]),20)
                    self.assertEqual(Decimal(r["totals"]["unallocated_cost"]),0)
                    self.assertEqual(r["totals"]["quantity_by_unit"],{unit:"6"})
                    self.assertEqual(configured_metrics(req,contract(code))["scale"]["value"],"4")
                    self.assertEqual(req.objects[0].kind,kind)
