import asyncio
import copy
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from analysis_terminal import trade_guidance as guidance
from analysis_terminal.entry_band import diagnose


def scenario(direction='LONG', target=104, roll=101, stage='CONFIRMATION_WAIT'):
    sign = 1 if direction == 'LONG' else -1
    stop = 100-sign*5
    target = target if direction == 'LONG' else 200-target
    roll = roll if direction == 'LONG' else 200-roll
    return dict(ticker='EXAMPLEUSDC', direction=direction, stage=stage, rr=abs(target-100)/5,
                entry_reference=100, stop_loss=stop, take_profit=target, tp1_2r=100+sign*10,
                entry_band=diagnose(direction,stop,target,roll,2))


class TradeGuidanceTests(unittest.TestCase):
    def test_waiting_for_confirmation_cannot_fix_no_overlap(self):
        for direction in ('LONG','SHORT'):
            row=scenario(direction);before=copy.deepcopy(row)
            result=guidance.describe(row,'WAIT FOR 15M CLOSE')
            self.assertEqual(result['action'],'WAIT FOR STRUCTURE')
            self.assertFalse(result['changes_ready_rules'])
            self.assertIsNone(result['first_target'])
            self.assertEqual(row,before)

    def test_equal_roll_and_rr_boundary_still_has_no_strict_confirmation(self):
        for direction in ('LONG','SHORT'):
            row=scenario(direction,target=110,roll=100)
            self.assertEqual(guidance.describe(row,'WAIT FOR RETEST')['status'],'STRUCTURE_CONFLICT')

    def test_compatible_structure_at_wrong_price_waits_for_rr_and_confirmation(self):
        for direction in ('LONG','SHORT'):
            row=scenario(direction,target=104,roll=97)
            self.assertEqual(row['entry_band']['status'],'COMPATIBLE')
            self.assertEqual(guidance.describe(row,'WAIT FOR 15M CLOSE')['action'],'WAIT FOR RR')

    def test_ready_and_qualified_waits_keep_existing_actions_and_targets(self):
        for direction in ('LONG','SHORT'):
            for stage,action in [('READY','ENTER'),('CONFIRMATION_WAIT','WAIT FOR 15M CLOSE'),('RETEST_WAIT','WAIT FOR RETEST')]:
                row=scenario(direction,target=110,roll=99,stage=stage)
                result=guidance.describe(row,action)
                self.assertEqual(result['action'],action)
                self.assertEqual(result['first_target'],row['take_profit'])

    def test_invalid_or_nonfinite_targets_cannot_be_first_profit(self):
        for direction in ('LONG','SHORT'):
            for value in (None,0,-1,float('nan'),float('inf'),100):
                row=scenario(direction,target=120,roll=99);row['tp1_2r']=value
                self.assertIsNone(guidance.valid_first_target(row))
            row=scenario(direction,target=120,roll=99);row['stop_loss']=100
            self.assertIsNone(guidance.valid_first_target(row))
            row=scenario(direction);row['entry_band']['status']='COMPATIBLE'
            self.assertEqual(guidance.describe(row,'ENTER')['action'],'SKIP')

    def test_chart_guidance_does_not_rewrite_frozen_stage_or_raw_target(self):
        from analysis_terminal import server
        row=scenario();contract=SimpleNamespace(contract_name=row['ticker'],contract_id='1')
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'1':contract})), \
             patch.object(server,'fetch_snapshots',AsyncMock(return_value={})), \
             patch.object(server,'analyze_contract',return_value=copy.deepcopy(row)):
            result=asyncio.run(server.chart_api(ticker=row['ticker'],limit_4h=80,limit_15m=120))
        analysis=result['analysis']
        for key in row:self.assertEqual(analysis[key],row[key])
        self.assertEqual(analysis['action'],'WAIT FOR STRUCTURE')
        self.assertIsNone(result['levels']['tp1'])
        self.assertEqual(result['levels']['target'],row['take_profit'])

    def test_analyze_api_adds_guidance_without_mutating_analyzer_result(self):
        from analysis_terminal import server
        row=scenario();before=copy.deepcopy(row)
        contract=SimpleNamespace(contract_name=row['ticker'],contract_id='1')
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'1':contract})), \
             patch.object(server,'fetch_snapshots',AsyncMock(return_value={})), \
             patch.object(server,'analyze_contract',return_value=row):
            result=asyncio.run(server.analyze_api(ticker=row['ticker']))
        self.assertEqual(row,before)
        self.assertEqual(result['trade_guidance']['action'],'WAIT FOR STRUCTURE')
        self.assertIsNone(result['trade_guidance']['first_target'])
        self.assertEqual({k:v for k,v in result.items() if k!='trade_guidance'},before)

    def test_shipped_ui_matches_backend_guidance_and_preserves_copy_targets(self):
        cases=[]
        for direction in ('LONG','SHORT'):
            for target in (104,110,120):
                for roll in (97,99,100,101):
                    for stage,action in [('CONFIRMATION_WAIT','WAIT FOR 15M CLOSE'),('RETEST_WAIT','WAIT FOR RETEST'),('RR_WAIT','SKIP')]:
                        row=scenario(direction,target,roll,stage)
                        cases.append(dict(row=row,expected=guidance.describe(row,action)))
        proc=subprocess.run(['node',str(Path(__file__).with_suffix('.js'))],input=json.dumps(cases),text=True,capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr)


if __name__=='__main__':unittest.main()
