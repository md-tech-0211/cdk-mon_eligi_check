#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { EligibilityStack } from '../lib/cdk-eligibility-stack';

const app = new cdk.App();
new EligibilityStack(app, 'EligibilitySetupStack', {
  
});