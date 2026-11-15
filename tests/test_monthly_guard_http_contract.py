"""Local integration-only contract for the thin FastAPI wrapper; no application writes."""
from dataclasses import replace
from typing import Annotated
import unittest

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from costing_monthly_guard import (
    MonthlyPreviewIncomparable, MonthlyPreviewUnavailable, MonthlyRevisionPin, guarded_monthly_preview,
)
from test_monthly_guard import Authority
import test_monthly_guard as fixture_contract
from test_month_comparison import _OWNER

ID = Annotated[str, Query(min_length=32,max_length=32,pattern=r"^[0-9a-f]{32}$")]
REVISION = Annotated[str, Query(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")]
ALLOWED={"current_analysis_id","current_revision","previous_analysis_id","previous_revision"}
NO_STORE = {"Cache-Control":"no-store"}

def app_for(local_snapshots, authority):
    app=FastAPI()
    app.state.costing_monthly_binding_provider=authority

    def authenticate(token: Annotated[str | None,Header()] = None):
        try:
            return {"test-session-a":_OWNER,"test-session-b":"tenant-b"}[token]
        except KeyError:
            raise HTTPException(401,"Unauthorized",headers=NO_STORE) from None

    def owned(analysis_id, owner):
        # Production uses _owned_snapshot and its session-derived workspace.
        for item in local_snapshots:
            if (item["context"]["owner_external_id"],item["analysis_id"]) == (owner,analysis_id):
                return item
        raise HTTPException(404,"any internal detail")

    @app.get("/isolated/monthly-preview")
    def preview(request: Request, current_analysis_id: ID,current_revision: REVISION,
                previous_analysis_id: ID,previous_revision: REVISION,
                owner: str=Depends(authenticate)):
        query_keys=[k for k,_ in request.query_params.multi_items()]
        if len(query_keys)!=len(ALLOWED) or set(query_keys)!=ALLOWED:
            raise HTTPException(422,"Invalid comparison request",headers=NO_STORE)
        provider=getattr(request.app.state,"costing_monthly_binding_provider",None)
        try:
            if provider is None:
                raise MonthlyPreviewUnavailable
            current=owned(current_analysis_id,owner)
            previous=owned(previous_analysis_id,owner)
            result=guarded_monthly_preview(
                current_snapshot=current,previous_snapshot=previous,expected_owner=owner,authority=provider,
                current_pin=MonthlyRevisionPin(current_analysis_id,current_revision),
                previous_pin=MonthlyRevisionPin(previous_analysis_id,previous_revision),
            )
        except MonthlyPreviewIncomparable:
            raise HTTPException(422,"Reports are not comparable",headers=NO_STORE) from None
        except (HTTPException,MonthlyPreviewUnavailable):
            raise HTTPException(404,"Comparison unavailable",headers=NO_STORE) from None
        return JSONResponse(result,headers=NO_STORE)
    return app

class HttpContract(unittest.TestCase):
    def setUp(self):
        state=fixture_contract.GuardContract()
        state.setUp()
        self.addCleanup(state.doCleanups)
        self.app=app_for([state.current,state.previous],state.authority)
        self.client=TestClient(self.app)
        self.auth={"token":"test-session-a"}
        self.state=state
        self.params={
            "current_analysis_id":state.current_pin.analysis_id,"current_revision":state.current_pin.revision,
            "previous_analysis_id":state.previous_pin.analysis_id,"previous_revision":state.previous_pin.revision,
        }

    def get(self,params=None,headers=None):
        return self.client.get("/isolated/monthly-preview",params=self.params if params is None else params,
                               headers=self.auth if headers is None else headers)

    def test_success_status_and_privacy(self):
        r=self.get()
        self.assertEqual(200,r.status_code)
        self.assertEqual("no-store",r.headers["Cache-Control"])
        self.assertEqual("30.00",r.json()["comparable_variance"])
        self.assertFalse(r.json()["can_claim_complete"])
        self.assertNotIn("policy_sha256", r.json())

    def test_unavailable_envelopes_are_identical(self):
        results=[self.get({**self.params,"previous_analysis_id":"f"*32}),
                 self.get({**self.params,"previous_revision":"f"*64}),
                 self.get(headers={"token":"test-session-b"})]
        self.app.state.costing_monthly_binding_provider=Authority({})
        results.append(self.get())
        self.app.state.costing_monthly_binding_provider=None
        results.append(self.get())
        self.assertEqual({(404,b'{"detail":"Comparison unavailable"}',"no-store")},
                         {(r.status_code,r.content,r.headers["Cache-Control"]) for r in results})

    def test_identity_claims_extra_query_and_duplicate_pin_rejected(self):
        for extra in (("owner",_OWNER),("policy_sha256","a"*64),("book_uid","not trusted"),
                      ("current_revision",self.params["current_revision"])):
            self.assertEqual(422,self.get(list(self.params.items())+[extra]).status_code)
        self.assertEqual(401,self.get(headers={}).status_code)

    def test_authenticated_but_incomparable(self):
        batch=self.state.batch
        self.app.state.costing_monthly_binding_provider=Authority(
            replace(batch,current=replace(batch.current,book_uid="different-book")))
        r=self.get()
        self.assertEqual(422,r.status_code)
        self.assertEqual({"detail":"Reports are not comparable"},r.json())

if __name__ == "__main__":
    unittest.main()
