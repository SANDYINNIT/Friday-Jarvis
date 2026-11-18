"""One real pinned-SDK mock-stream test; no provider/server/device use."""
from pathlib import Path
import importlib.metadata
import inspect
import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'packaging'))
from tokenizer_cache import verify

class SDKOfflineTest(unittest.TestCase):
    def test_pinned_sdk_import_and_stream_without_external_connections(self):
        clean={k:v for k,v in os.environ.items() if not (k.startswith(('FRIDAY_','GROQ_','GEMINI_','OPENROUTER_','MODAL_','DEEPGRAM_','OLLAMA_','CUSTOM_TIKTOKEN')) or 'API_KEY' in k)}
        with tempfile.TemporaryDirectory(prefix='friday-sdk-fixture-') as home:
            clean.update(HOME=home,USERPROFILE=home,LITELLM_LOCAL_MODEL_COST_MAP='True',SCARF_NO_ANALYTICS='true',HF_HUB_OFFLINE='1',PYTHON_DOTENV_DISABLED='1',LITELLM_MODE='PRODUCTION',CUSTOM_TIKTOKEN_CACHE_DIR=str(verify()))
            external=[];original=socket.socket.connect
            def connect(sock,address):
                frame=sys._getframe(1)
                while frame:
                    if frame.f_code.co_name in ('socketpair','_fallback_socketpair') and Path(frame.f_code.co_filename).resolve()==Path(socket.__file__).resolve():
                        return original(sock,address)
                    frame=frame.f_back
                external.append(1);raise AssertionError('External connection forbidden')
            with patch.dict(os.environ,clean,clear=True),patch.object(socket.socket,'connect',connect):
                import litellm
                self.assertEqual(importlib.metadata.version('litellm'),'1.100.0')
                signature=inspect.signature(litellm.completion)
                for name in ('model','messages','stream','timeout','api_key','max_tokens'):self.assertIn(name,signature.parameters)
                response=litellm.completion(model='openai/gpt-4o-mini',api_key='synthetic',messages=[{'role':'user','content':'Synthetic question'}],stream=True,mock_response='Synthetic SDK answer.')
                text=''.join(c['choices'][0]['delta'].get('content') or '' for c in response)
                self.assertEqual(text,'Synthetic SDK answer.');self.assertEqual(external,[])
                # Exercise actual SDK chunk classes through the shipped Linux Session,
                # not just hand-authored dictionaries.
                import asyncio
                sys.path.insert(0,str(ROOT/'software'))
                import linux_main
                os.environ['FRIDAY_CLOUD_BRAIN']='0'
                os.environ['FRIDAY_EXHAUSTION_LEDGER']=str(Path(home)/'cooldowns.json')
                def mocked_completion(**kwargs):
                    return litellm.completion(**kwargs,mock_response='SDK terminal answer.')
                session=linux_main.Session(Path(home)/'session',mocked_completion)
                from litellm.llms.ollama.common_utils import OllamaModelInfo
                # LiteLLM's documented mock_response does not suppress Ollama model-info
                # lookup. Supply a fake metadata adapter as well; never contact a local service.
                metadata={'key':'qwen3:8b','litellm_provider':'ollama','mode':'chat','max_tokens':8192,'max_input_tokens':8192,'max_output_tokens':512,'input_cost_per_token':0.0,'output_cost_per_token':0.0,'supports_function_calling':False}
                output=[]
                with patch.object(OllamaModelInfo,'get_runtime_model_info',return_value=metadata):
                    result=asyncio.run(session.ask('Synthetic terminal question',output.append))
                self.assertEqual(result['outcome'],'completed')
                self.assertEqual(''.join(output),'SDK terminal answer.')
                self.assertEqual(external,[])

if __name__=='__main__':unittest.main()
