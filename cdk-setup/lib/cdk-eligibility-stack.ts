import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as path from 'path';

export class EligibilityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    //  IAM Role
    const clinicalRole = new iam.Role(this, 'LambdaClinicalRoleV2', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      roleName: 'LambdaClinicalPolicy-v2', 
      description: 'Allows Lambda to call Bedrock, S3, and CloudWatch',
    });

    
    clinicalRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'));
    clinicalRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonS3ReadOnlyAccess'));
    clinicalRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonBedrockFullAccess'));

    //Lambda Function
    const eligibilityLambda = new lambda.Function(this, 'EligibilityCheckerFunction', {
      functionName: 'monday-eligibility-checker-v2',
      
      runtime: lambda.Runtime.PYTHON_3_12, 
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../eligibility-lambda-code')), 
      role: clinicalRole,
      timeout: cdk.Duration.seconds(180),
      memorySize: 256,
      environment: {
        MONDAY_API_KEY: 'eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjYzMTg0NjQ2MSwiYWFpIjoxMSwidWlkIjoxMDAyNTY3OTYsImlhZCI6IjIwMjYtMDMtMTFUMTU6NTE6MTcuODk1WiIsInBlciI6Im1lOndyaXRlIiwiYWN0aWQiOjEzNjcwMDMxLCJyZ24iOiJ1c2UxIn0.i4gZAtrYCcJVQkYiJGGGJgrNDyu1L9HPFIZKEyi3upA',
      },
    });

    // Function URL
    const functionUrl = eligibilityLambda.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
    });

    //API Gateway 
    const lambdaIntegration = new HttpLambdaIntegration('EligibilityIntegration', eligibilityLambda);

    const httpApi = new apigwv2.HttpApi(this, 'EligibilityWebhookApi', {
      apiName: 'eligibility-webhook-v2', 
    });

    
    httpApi.addRoutes({
      path: '/eligibility',
      methods: [apigwv2.HttpMethod.POST],
      integration: lambdaIntegration,
    });

    
    new cdk.CfnOutput(this, 'EligibilityFunctionUrl', { 
      value: functionUrl.url,
      description: 'The public Function URL'
    });
    new cdk.CfnOutput(this, 'EligibilityApiUrl', { 
      value: httpApi.url + 'eligibility',
      description: 'The API Gateway Webhook URL'
    });
  }
}