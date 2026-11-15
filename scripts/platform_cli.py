"""Offline entry point for the registered industry-neutral platform API."""
import argparse,json
from cost_core.registry import AnalysisRequest,to_primitive
from cost_platform import create_service

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--list-plugins',action='store_true')
    parser.add_argument('--list-datasets',action='store_true')
    parser.add_argument('--dataset',default='pharma-sample-v1')
    parser.add_argument('--plugin')
    parser.add_argument('--object',dest='cost_object')
    parser.add_argument('--period')
    parser.add_argument('--organization')
    parser.add_argument('--preview',action='store_true')
    args=parser.parse_args();service=create_service()
    if args.list_plugins:result=to_primitive(service.registry.list())
    elif args.list_datasets:result=service.datasets()
    else:
        if not args.cost_object or not args.period:parser.error('--object and --period are required')
        request=AnalysisRequest(dataset_id=args.dataset,plugin_id=args.plugin,cost_object=args.cost_object,
                                period=args.period,organization=args.organization)
        result=service.preview_report(request) if args.preview else service.analyze_payload(request)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
