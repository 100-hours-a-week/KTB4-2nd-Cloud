# Independent state; no foundation/Secret/V2 state reads or resource changes.
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["483175530259"]
  default_tags {
    tags = {
      Project     = "yeodam"
      Version     = "v3"
      Environment = "foundation"
      ManagedBy   = "Terraform"
      Issue       = "527"
    }
  }
}
locals {
  cluster_name = "yeodam-v3-foundation"
  cluster_arn  = "arn:aws:eks:ap-northeast-2:483175530259:cluster/${local.cluster_name}"
  name         = "${local.cluster_name}-karpenter"
}
resource "aws_iam_role" "controller" {
  name = "${local.name}-controller"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:SourceAccount"                         = "483175530259"
          "aws:RequestTag/kubernetes-namespace"       = "karpenter"
          "aws:RequestTag/kubernetes-service-account" = "karpenter"
        }
        ArnEquals = { "aws:SourceArn" = local.cluster_arn }
      }
    }]
  })
}
resource "aws_eks_pod_identity_association" "controller" {
  cluster_name    = local.cluster_name
  namespace       = "karpenter"
  service_account = "karpenter"
  role_arn        = aws_iam_role.controller.arn
}
resource "aws_iam_role" "node" {
  name = "${local.name}-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
# CNI already uses its own IRSA role; do not grant ENI mutation to nodes.
resource "aws_iam_role_policy_attachment" "node" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonSSMManagedInstanceCore"])
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}
# Future EC2NodeClass must use spec.instanceProfile, not spec.role.
# Terraform owns this profile; controller cannot create or mutate IAM profiles.
resource "aws_iam_instance_profile" "node" {
  name = "${local.name}-node"
  role = aws_iam_role.node.name
}
resource "aws_eks_access_entry" "node" {
  cluster_name  = local.cluster_name
  principal_arn = aws_iam_role.node.arn
  type          = "EC2_LINUX"
}
resource "aws_iam_policy" "lifecycle" {
  name   = "${local.name}-lifecycle"
  policy = file("${path.module}/policies/lifecycle.json")
}
resource "aws_iam_role_policy_attachment" "lifecycle" {
  role       = aws_iam_role.controller.name
  policy_arn = aws_iam_policy.lifecycle.arn
}
resource "aws_iam_policy" "discovery" {
  name = "${local.name}-discovery"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RegionalEC2Read"
        Effect    = "Allow"
        Action    = ["ec2:DescribeCapacityReservations", "ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeInstanceTypes", "ec2:DescribeLaunchTemplates", "ec2:DescribePlacementGroups", "ec2:DescribeSecurityGroups", "ec2:DescribeSpotPriceHistory", "ec2:DescribeSubnets"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = "ap-northeast-2" } }
      },
      { Sid = "PricingRead", Effect = "Allow", Action = "pricing:GetProducts", Resource = "*" },
      { Sid = "PublicAMIRead", Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:ap-northeast-2::parameter/aws/service/eks/optimized-ami/*" },
      { Sid = "ClusterRead", Effect = "Allow", Action = "eks:DescribeCluster", Resource = local.cluster_arn },
      # The pinned controller periodically lists its managed-profile path even
      # with spec.instanceProfile. IAM ListInstanceProfiles requires Resource=*.
      # Our profile remains outside that path; no profile mutation is delegated.
      { Sid = "ProfileList", Effect = "Allow", Action = "iam:ListInstanceProfiles", Resource = "*" },
      { Sid = "ProfileRead", Effect = "Allow", Action = "iam:GetInstanceProfile", Resource = aws_iam_instance_profile.node.arn },
      {
        Sid       = "PassDedicatedNodeRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.node.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      { Sid = "InterruptionRead", Effect = "Allow", Action = ["sqs:GetQueueUrl", "sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = aws_sqs_queue.interruption.arn }
    ]
  })
}
resource "aws_iam_role_policy_attachment" "discovery" {
  role       = aws_iam_role.controller.name
  policy_arn = aws_iam_policy.discovery.arn
}
output "controller_role_arn" { value = aws_iam_role.controller.arn }
output "node_instance_profile" { value = aws_iam_instance_profile.node.name }
output "interruption_queue_name" { value = aws_sqs_queue.interruption.name }
