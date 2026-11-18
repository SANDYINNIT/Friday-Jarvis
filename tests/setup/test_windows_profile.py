"""Actual scoped Windows profile ACL/config commands, never a native app launch."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
@unittest.skipUnless(os.name=='nt','Windows profile fixture only')
class WindowsProfileTest(unittest.TestCase):
 def test_actual_wrapper_private_profile_and_passive_check(self):
  with tempfile.TemporaryDirectory(prefix='friday-windows-profile-') as temporary:
   home=Path(temporary);(home/'Local').mkdir()
   env={k:v for k,v in os.environ.items() if not (k.startswith(('FRIDAY_','GROQ_','GEMINI_','MODAL_','OPENROUTER_','OLLAMA_','DEEPGRAM_')) or 'API_KEY' in k)}
   env.update(HOME=str(home),USERPROFILE=str(home),LOCALAPPDATA=str(home/'Local'),PYTHON_DOTENV_DISABLED='1')
   script=str(ROOT/'run-windows.ps1').replace("'","''")
   def command(arguments):
    code="& '"+script+"' "+arguments
    return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',code],env=env,capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
   profile=home/'Local/FRIDAY'
   result=command('-Check');self.assertNotEqual(result.returncode,0);self.assertFalse(profile.exists())
   result=command("-Configure -ConfigArgs @('init')");self.assertEqual(result.returncode,0,result.stderr)
   config=profile/'routes.json';initial=json.loads(config.read_text());self.assertEqual(initial['bindings'],[]);self.assertEqual(initial['chains'],{'brain':[],'vision':[]})
   result=command("-Configure -ConfigArgs @('add','ollama','synthetic-model:1','--roles','brain,vision','--deployment','local-test')");self.assertEqual(result.returncode,0,result.stderr)
   data=json.loads(config.read_text());id=data['bindings'][0]['id'];self.assertEqual(data['chains']['brain'],[])
   result=command("-Configure -ConfigArgs @('chain','brain','"+id+"')");self.assertEqual(result.returncode,0,result.stderr)
   before={p.name:p.read_bytes() for p in profile.iterdir() if p.is_file()}
   result=command('-Check');self.assertEqual(result.returncode,0,result.stderr);self.assertIn('health',result.stdout);self.assertIn('unknown',result.stdout)
   after={p.name:p.read_bytes() for p in profile.iterdir() if p.is_file()};self.assertEqual(before,after);self.assertFalse(list(profile.rglob('*.db')))
   code="Import-Module -Name ([IO.Path]::Combine($PSHOME,'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1')); $d=Get-Acl -LiteralPath '"+str(profile).replace("'","''")+"'; if (!$d.AreAccessRulesProtected) {exit 3}; if ($d.Access.Count -ne 3) {exit 4}"
   check=subprocess.run(['powershell.exe','-NoProfile','-Command',code],env=env,capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW);self.assertEqual(check.returncode,0,check.stderr)
   env['FRIDAY_USER_ROUTES']=str(home/'outside.json');result=command('-Check');self.assertNotEqual(result.returncode,0);self.assertFalse((home/'outside.json').exists())

if __name__=='__main__':unittest.main()
