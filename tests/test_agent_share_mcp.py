import json
from pathlib import Path
import pytest
from tools import ap_vibe_mcp


def test_mcp_sharing_keeps_images_in_files_and_does_not_overwrite(tmp_path,monkeypatch):
    calls=[]
    bundle={'schema':'ap-vibe.agents.v1','agents':[{'source_id':'x'}],'appearances':[{'png_base64':'not-returned-to-model'}]}
    def call(route,body):
        calls.append((route,body))
        return {'ok':True,'bundle':bundle} if route.endswith('/export') else {'ok':True,'agents':[]}
    monkeypatch.setattr(ap_vibe_mcp.task_client,'call',call)
    path=tmp_path/'partners.json'
    result=ap_vibe_mcp.invoke('ap_vibe_agent_share_export',{'agent_ids':['x'],'output_path':str(path)})
    assert result['agent_count']==1 and 'bundle' not in result and 'not-returned-to-model' not in json.dumps(result)
    with pytest.raises(ValueError):ap_vibe_mcp.invoke('ap_vibe_agent_share_export',{'agent_ids':['x'],'output_path':str(path)})
    ap_vibe_mcp.invoke('ap_vibe_agent_share_preview',{'file_path':str(path)})
    assert calls[-1]==('agents/share/preview',{'bundle':bundle})
    ap_vibe_mcp.invoke('ap_vibe_agent_share_import',{'request_id':'stable','file_path':str(path),'selected_ids':['x']})
    assert calls[-1][1]=={'request_id':'stable','selected_ids':['x'],'bundle':bundle}
