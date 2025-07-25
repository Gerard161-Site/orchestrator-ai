
"""
Enhanced Two-Tiered Multi-Agent Orchestration System with SSH Capabilities
==========================================================================

This enhanced orchestrator provides a two-tiered system that can handle:
1. Self-contained repositories with ai-module.yaml configurations
2. Traditional repositories with task prompts

Key Features:
- Auto-detection of repository type (ai-module.yaml vs task prompts)
- SSH command execution with banking-grade security
- Unified workflow handling for both approaches
- Comprehensive audit logging and security monitoring
- Advanced multi-agent collaboration patterns
- Real-time deployment and monitoring
"""

import asyncio
import json
import logging
import os
import subprocess
import time
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional, TypedDict, Union
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path

from crewai import Agent, Task, Crew
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage
from langchain.callbacks import get_openai_callback
from langchain.evaluation import load_evaluator
from langchain_community.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter

from context_manager import EnhancedContextManager
from mcp_bridge import EnhancedMCPBridge
from ssh_manager import EnhancedSSHManager, SSHConnection, SecurityLevel
from ai_module_parser import AIModuleParser, AIModuleConfig, ModuleType
from security import SecurityAuditLogger, create_security_event, EventType, get_audit_logger
from dotenv import load_dotenv

load_dotenv()

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('orchestrator.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

class WorkflowType(Enum):
    AI_MODULE = "ai_module"  # Self-contained with ai-module.yaml
    TASK_PROMPT = "task_prompt"  # Traditional with task prompts

class DeploymentStatus(Enum):
    PENDING = "pending"
    BUILDING = "building"
    TESTING = "testing"
    DEPLOYING = "deploying"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"

@dataclass
class WorkflowState:
    workflow_id: str
    workflow_type: WorkflowType
    status: DeploymentStatus
    repository_url: str
    target_host: str
    project_path: str
    config: Optional[AIModuleConfig] = None
    task_prompt: Optional[str] = None
    environment_variables: Dict[str, str] = None
    deployment_logs: List[str] = None
    created_at: datetime = None
    updated_at: datetime = None

class EnhancedTwoTierOrchestrator:
    """Enhanced Two-Tiered Multi-Agent Orchestration System"""
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        
        # Initialize components
        self.context_manager = EnhancedContextManager()
        self.mcp_bridge = EnhancedMCPBridge()
        self.ssh_manager = EnhancedSSHManager()
        self.ai_module_parser = AIModuleParser()
        self.audit_logger = get_audit_logger()
        
        # Initialize AI components
        self.llm = ChatOpenAI(
            model="gpt-4",
            temperature=0.1,
            openai_api_key=os.getenv("OPENAI_API_KEY")
        )
        
        # Workflow tracking
        self.active_workflows: Dict[str, WorkflowState] = {}
        self.rollback_stack: List[Dict[str, Any]] = []
        
        # SSH configuration for mcp.xplaincrypto.ai
        self.default_ssh_config = SSHConnection(
            host=os.getenv("DEPLOY_HOST", "mcp.xplaincrypto.ai"),
            port=int(os.getenv("DEPLOY_PORT", "22")),
            username=os.getenv("DEPLOY_USER", "root"),
            key_path=os.getenv("DEPLOY_KEY_PATH", "/root/.ssh/id_rsa"),
            security_level=SecurityLevel.HIGH
        )
        
        logger.info("Enhanced Two-Tier Orchestrator initialized")
    
    async def detect_workflow_type(self, repository_path: str) -> WorkflowType:
        """Auto-detect workflow type based on repository structure"""
        
        repo_path = Path(repository_path)
        ai_module_file = repo_path / "ai-module.yaml"
        
        if ai_module_file.exists():
            logger.info(f"Detected AI module workflow: {repository_path}")
            return WorkflowType.AI_MODULE
        else:
            logger.info(f"Detected task prompt workflow: {repository_path}")
            return WorkflowType.TASK_PROMPT
    
    async def process_ai_module_workflow(
        self, 
        workflow_state: WorkflowState
    ) -> Dict[str, Any]:
        """Process self-contained AI module workflow"""
        
        try:
            workflow_state.status = DeploymentStatus.BUILDING
            
            # Parse ai-module.yaml
            ai_module_path = Path(workflow_state.project_path) / "ai-module.yaml"
            config = self.ai_module_parser.parse_file(str(ai_module_path))
            workflow_state.config = config
            
            logger.info(f"Processing AI module: {config.name} v{config.version}")
            
            # Log security event
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.CONFIGURATION_CHANGE,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="ai_module_deployment",
                result="started",
                details={"module_name": config.name, "version": config.version}
            ))
            
            # Install system dependencies
            if config.system_dependencies:
                deps_command = f"apt-get update && apt-get install -y {' '.join(config.system_dependencies)}"
                deps_result = await self.ssh_manager.execute_command(
                    self.default_ssh_config, deps_command
                )
                if not deps_result.success:
                    raise Exception(f"Failed to install system dependencies: {deps_result.stderr}")
            
            # Build the application
            workflow_state.status = DeploymentStatus.BUILDING
            build_result = await self.ssh_manager.execute_command(
                self.default_ssh_config,
                f"cd {workflow_state.project_path} && {config.build_command}"
            )
            
            if not build_result.success:
                workflow_state.status = DeploymentStatus.FAILED
                raise Exception(f"Build failed: {build_result.stderr}")
            
            # Run tests if specified
            if config.test_command:
                workflow_state.status = DeploymentStatus.TESTING
                test_result = await self.ssh_manager.execute_command(
                    self.default_ssh_config,
                    f"cd {workflow_state.project_path} && {config.test_command}"
                )
                
                if not test_result.success:
                    logger.warning(f"Tests failed: {test_result.stderr}")
                    # Continue deployment despite test failures (configurable)
            
            # Deploy the application
            workflow_state.status = DeploymentStatus.DEPLOYING
            
            # Generate deployment script based on target
            deployment_script = self._generate_deployment_script(config, workflow_state)
            
            deploy_result = await self.ssh_manager.execute_script(
                self.default_ssh_config,
                deployment_script,
                f"deploy_{config.name}.sh"
            )
            
            if not deploy_result.success:
                workflow_state.status = DeploymentStatus.FAILED
                raise Exception(f"Deployment failed: {deploy_result.stderr}")
            
            # Setup health monitoring
            if config.health_check.enabled:
                await self._setup_health_monitoring(config, workflow_state)
            
            workflow_state.status = DeploymentStatus.RUNNING
            workflow_state.updated_at = datetime.now()
            
            # Log successful deployment
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.CONFIGURATION_CHANGE,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="ai_module_deployment",
                result="success",
                details={
                    "module_name": config.name,
                    "version": config.version,
                    "port": config.port,
                    "deployment_target": config.deployment_target.value
                }
            ))
            
            return {
                "success": True,
                "workflow_id": workflow_state.workflow_id,
                "module_name": config.name,
                "version": config.version,
                "status": workflow_state.status.value,
                "endpoint": f"http://{self.default_ssh_config.host}:{config.port}",
                "health_check": f"http://{self.default_ssh_config.host}:{config.port}{config.health_check.endpoint}"
            }
            
        except Exception as e:
            workflow_state.status = DeploymentStatus.FAILED
            workflow_state.updated_at = datetime.now()
            
            # Log failure
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.SYSTEM_ERROR,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="ai_module_deployment",
                result="failed",
                details={"error": str(e)}
            ))
            
            logger.error(f"AI module workflow failed: {e}")
            return {
                "success": False,
                "workflow_id": workflow_state.workflow_id,
                "error": str(e),
                "status": workflow_state.status.value
            }
    
    async def process_task_prompt_workflow(
        self, 
        workflow_state: WorkflowState
    ) -> Dict[str, Any]:
        """Process traditional task prompt workflow"""
        
        try:
            workflow_state.status = DeploymentStatus.BUILDING
            
            logger.info(f"Processing task prompt workflow: {workflow_state.task_prompt[:100]}...")
            
            # Log security event
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.CONFIGURATION_CHANGE,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="task_prompt_deployment",
                result="started",
                details={"task_prompt": workflow_state.task_prompt[:200]}
            ))
            
            # Create AI agents for task execution
            agents = await self._create_task_agents()
            
            # Analyze repository structure
            analysis_result = await self._analyze_repository_structure(workflow_state.project_path)
            
            # Generate deployment strategy
            deployment_strategy = await self._generate_deployment_strategy(
                workflow_state.task_prompt,
                analysis_result
            )
            
            # Execute deployment tasks
            workflow_state.status = DeploymentStatus.DEPLOYING
            
            for task in deployment_strategy["tasks"]:
                task_result = await self.ssh_manager.execute_command(
                    self.default_ssh_config,
                    task["command"]
                )
                
                if not task_result.success and task.get("required", True):
                    workflow_state.status = DeploymentStatus.FAILED
                    raise Exception(f"Required task failed: {task['name']} - {task_result.stderr}")
                
                workflow_state.deployment_logs.append(
                    f"Task: {task['name']} - {'SUCCESS' if task_result.success else 'FAILED'}"
                )
            
            workflow_state.status = DeploymentStatus.RUNNING
            workflow_state.updated_at = datetime.now()
            
            # Log successful deployment
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.CONFIGURATION_CHANGE,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="task_prompt_deployment",
                result="success",
                details={"tasks_executed": len(deployment_strategy["tasks"])}
            ))
            
            return {
                "success": True,
                "workflow_id": workflow_state.workflow_id,
                "status": workflow_state.status.value,
                "deployment_strategy": deployment_strategy,
                "logs": workflow_state.deployment_logs,
                "endpoint": deployment_strategy.get("endpoint")
            }
            
        except Exception as e:
            workflow_state.status = DeploymentStatus.FAILED
            workflow_state.updated_at = datetime.now()
            
            # Log failure
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.SYSTEM_ERROR,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="task_prompt_deployment",
                result="failed",
                details={"error": str(e)}
            ))
            
            logger.error(f"Task prompt workflow failed: {e}")
            return {
                "success": False,
                "workflow_id": workflow_state.workflow_id,
                "error": str(e),
                "status": workflow_state.status.value
            }
    
    async def run_unified_workflow(
        self,
        repository_url: str,
        task_prompt: Optional[str] = None,
        target_host: Optional[str] = None,
        environment_variables: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Run unified workflow that auto-detects and handles both types"""
        
        # Generate workflow ID
        workflow_id = hashlib.sha256(
            f"{repository_url}{time.time()}".encode()
        ).hexdigest()[:16]
        
        # Clone repository with DEPLOY flag logic
        project_path = f"/tmp/projects/{workflow_id}"
        deploy_mode = os.getenv("DEPLOY", "false").lower() == "true"
        
        if deploy_mode:
            # Use SSH for deployment tasks
            print(f"[DEPLOY=true] Using SSH to clone repository {repository_url}")
            clone_result = await self.ssh_manager.execute_command(
                self.default_ssh_config,
                f"git clone {repository_url} {project_path}"
            )
            if not clone_result.success:
                return {
                    "success": False,
                    "error": f"Failed to clone repository: {clone_result.stderr}"
                }
        else:
            # Use local execution for simple coding tasks
            print(f"[DEPLOY=false] Using local git clone for repository {repository_url}")
            try:
                # import os already available at module level
                os.makedirs("/tmp/projects", exist_ok=True)
                result = subprocess.run(
                    f"git clone {repository_url} {project_path}",
                    shell=True, capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    return {
                        "success": False,
                        "error": f"Failed to clone repository: {result.stderr}"
                    }
                print(f"[DEPLOY=false] Local clone succeeded: {repository_url} -> {project_path}")
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                return {
                    "success": False,
                    "error": f"Failed to clone repository: Local execution error: {e}"
                }
        
        # Detect workflow type
        workflow_type = await self.detect_workflow_type(project_path)
        
        # Create workflow state
        workflow_state = WorkflowState(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            status=DeploymentStatus.PENDING,
            repository_url=repository_url,
            target_host=target_host or self.default_ssh_config.host,
            project_path=project_path,
            task_prompt=task_prompt,
            environment_variables=environment_variables or {},
            deployment_logs=[],
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        
        # Store workflow state
        self.active_workflows[workflow_id] = workflow_state
        
        try:
            # Process based on workflow type
            if workflow_type == WorkflowType.AI_MODULE:
                result = await self.process_ai_module_workflow(workflow_state)
            else:
                result = await self.process_task_prompt_workflow(workflow_state)
            
            return result
            
        except Exception as e:
            logger.error(f"Unified workflow failed: {e}")
            return {
                "success": False,
                "workflow_id": workflow_id,
                "error": str(e)
            }
    
    def _generate_deployment_script(
        self, 
        config: AIModuleConfig, 
        workflow_state: WorkflowState
    ) -> str:
        """Generate deployment script based on configuration"""
        
        script_parts = [
            "#!/bin/bash",
            "set -e",
            "",
            f"# Deployment script for {config.name} v{config.version}",
            f"cd {workflow_state.project_path}",
            ""
        ]
        
        # Environment variables
        if config.environment_variables:
            script_parts.append("# Set environment variables")
            for key, value in config.environment_variables.items():
                script_parts.append(f"export {key}='{value}'")
            script_parts.append("")
        
        # Install dependencies
        if config.dependencies:
            if config.deployment_target.value in ["docker"]:
                script_parts.extend([
                    "# Build Docker image",
                    f"docker build -t {config.name}:{config.version} .",
                    "",
                    "# Stop existing container",
                    f"docker stop {config.name} || true",
                    f"docker rm {config.name} || true",
                    "",
                    "# Run new container",
                    f"docker run -d --name {config.name} -p {config.port}:{config.port} {config.name}:{config.version}",
                    ""
                ])
            else:
                script_parts.extend([
                    "# Install dependencies",
                    config.build_command,
                    "",
                    "# Start application",
                    f"nohup {config.start_command} > app.log 2>&1 &",
                    f"echo $! > {config.name}.pid",
                    ""
                ])
        
        # Health check
        if config.health_check.enabled:
            script_parts.extend([
                "# Wait for application to start",
                "sleep 10",
                "",
                "# Health check",
                f"curl -f http://localhost:{config.port}{config.health_check.endpoint} || exit 1",
                ""
            ])
        
        script_parts.append("echo 'Deployment completed successfully'")
        
        return "\n".join(script_parts)
    
    async def _setup_health_monitoring(
        self, 
        config: AIModuleConfig, 
        workflow_state: WorkflowState
    ):
        """Setup health monitoring for deployed application"""
        
        monitoring_script = f"""#!/bin/bash
# Health monitoring script for {config.name}

while true; do
    if curl -f http://localhost:{config.port}{config.health_check.endpoint} > /dev/null 2>&1; then
        echo "$(date): {config.name} is healthy"
    else
        echo "$(date): {config.name} health check failed"
        # Optional: restart application
        # systemctl restart {config.name}
    fi
    sleep {config.health_check.interval}
done
"""
        
        # Upload and start monitoring script
        await self.ssh_manager.execute_script(
            self.default_ssh_config,
            monitoring_script,
            f"monitor_{config.name}.sh"
        )
        
        # Start monitoring in background
        await self.ssh_manager.execute_command(
            self.default_ssh_config,
            f"nohup /tmp/monitor_{config.name}.sh > /var/log/{config.name}_monitor.log 2>&1 &"
        )
    
    async def _create_task_agents(self) -> List[Agent]:
        """Create AI agents for task execution"""
        
        architect_agent = Agent(
            role="Solution Architect",
            goal="Design optimal deployment architecture",
            backstory="Expert in system architecture and deployment strategies",
            llm=self.llm,
            verbose=True
        )
        
        devops_agent = Agent(
            role="DevOps Engineer", 
            goal="Execute deployment and infrastructure tasks",
            backstory="Experienced in CI/CD, containerization, and cloud deployments",
            llm=self.llm,
            verbose=True
        )
        
        security_agent = Agent(
            role="Security Engineer",
            goal="Ensure secure deployment practices",
            backstory="Expert in application security and secure deployment practices",
            llm=self.llm,
            verbose=True
        )
        
        return [architect_agent, devops_agent, security_agent]
    
    async def _analyze_repository_structure(self, project_path: str) -> Dict[str, Any]:
        """Analyze repository structure - use local execution for simple tasks"""
        
        # Check if we should deploy or just code locally
        deploy_mode = os.getenv("DEPLOY", "false").lower() == "true"
        
        if deploy_mode:
            # Use SSH for actual deployment tasks
            print(f"[DEPLOY=true] Using SSH to analyze repository structure at {project_path}")
            analysis_result = await self.ssh_manager.execute_command(
                self.default_ssh_config,
                f"find {project_path} -type f -name '*.json' -o -name '*.py' -o -name '*.js' -o -name 'Dockerfile' -o -name 'requirements.txt' -o -name 'package.json' | head -20"
            )
            files = analysis_result.stdout.strip().split('\n') if analysis_result.success else []
        else:
            # Use local execution for simple coding tasks
            print(f"[DEPLOY=false] Using local execution to analyze repository structure at {project_path}")
            try:
                result = subprocess.run(
                    f"find {project_path} -type f -name '*.json' -o -name '*.py' -o -name '*.js' -o -name 'Dockerfile' -o -name 'requirements.txt' -o -name 'package.json' | head -20",
                    shell=True, capture_output=True, text=True, timeout=30
                )
                files = result.stdout.strip().split('\n') if result.returncode == 0 and result.stdout.strip() else []
                print(f"[DEPLOY=false] Local file analysis succeeded: found {len(files)} files")
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                print(f"[DEPLOY=false] Local file analysis failed: {e}")
                files = []
        
        # Detect technology stack
        tech_stack = []
        if any('package.json' in f for f in files):
            tech_stack.append('nodejs')
        if any('requirements.txt' in f for f in files):
            tech_stack.append('python')
        if any('Dockerfile' in f for f in files):
            tech_stack.append('docker')
        
        return {
            "files": files,
            "tech_stack": tech_stack,
            "has_dockerfile": any('Dockerfile' in f for f in files),
            "has_package_json": any('package.json' in f for f in files),
            "has_requirements": any('requirements.txt' in f for f in files)
        }
    async def _generate_deployment_strategy(
        self, 
        task_prompt: str, 
        analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate deployment strategy using AI agents"""
        
        # Create deployment tasks based on analysis
        tasks = []
        
        if analysis["has_dockerfile"]:
            tasks.extend([
                {
                    "name": "build_docker_image",
                    "command": f"cd {analysis.get('project_path', '.')} && docker build -t app:latest .",
                    "required": True
                },
                {
                    "name": "run_docker_container",
                    "command": "docker run -d -p 8000:8000 --name app app:latest",
                    "required": True
                }
            ])
        elif analysis["has_package_json"]:
            tasks.extend([
                {
                    "name": "install_npm_dependencies",
                    "command": "npm install",
                    "required": True
                },
                {
                    "name": "build_application",
                    "command": "npm run build",
                    "required": False
                },
                {
                    "name": "start_application",
                    "command": "nohup npm start > app.log 2>&1 &",
                    "required": True
                }
            ])
        elif analysis["has_requirements"]:
            tasks.extend([
                {
                    "name": "install_python_dependencies",
                    "command": "pip install -r requirements.txt",
                    "required": True
                },
                {
                    "name": "start_python_application",
                    "command": "nohup python app.py > app.log 2>&1 &",
                    "required": True
                }
            ])
        
        return {
            "strategy": "auto_detected",
            "tech_stack": analysis["tech_stack"],
            "tasks": tasks,
            "endpoint": "http://localhost:8000"
        }
    
    async def get_workflow_status(self, workflow_id: str) -> Dict[str, Any]:
        """Get status of a specific workflow"""
        
        if workflow_id not in self.active_workflows:
            return {"error": "Workflow not found"}
        
        workflow_state = self.active_workflows[workflow_id]
        
        return {
            "workflow_id": workflow_id,
            "workflow_type": workflow_state.workflow_type.value,
            "status": workflow_state.status.value,
            "repository_url": workflow_state.repository_url,
            "target_host": workflow_state.target_host,
            "created_at": workflow_state.created_at.isoformat(),
            "updated_at": workflow_state.updated_at.isoformat(),
            "deployment_logs": workflow_state.deployment_logs,
            "config": asdict(workflow_state.config) if workflow_state.config else None
        }
    
    async def list_active_workflows(self) -> List[Dict[str, Any]]:
        """List all active workflows"""
        
        workflows = []
        for workflow_id, workflow_state in self.active_workflows.items():
            workflows.append({
                "workflow_id": workflow_id,
                "workflow_type": workflow_state.workflow_type.value,
                "status": workflow_state.status.value,
                "repository_url": workflow_state.repository_url,
                "created_at": workflow_state.created_at.isoformat()
            })
        
        return workflows
    
    async def stop_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Stop a running workflow"""
        
        if workflow_id not in self.active_workflows:
            return {"success": False, "error": "Workflow not found"}
        
        workflow_state = self.active_workflows[workflow_id]
        
        try:
            # Stop application based on type
            if workflow_state.config:
                # AI module workflow
                stop_command = f"docker stop {workflow_state.config.name} || pkill -f '{workflow_state.config.start_command}'"
            else:
                # Task prompt workflow
                stop_command = "docker stop app || pkill -f 'python\\|node\\|npm'"
            
            stop_result = await self.ssh_manager.execute_command(
                self.default_ssh_config,
                stop_command
            )
            
            workflow_state.status = DeploymentStatus.STOPPED
            workflow_state.updated_at = datetime.now()
            
            # Log stop event
            self.audit_logger.log_event(create_security_event(
                event_type=EventType.CONFIGURATION_CHANGE,
                user_id="system",
                source_ip="localhost",
                resource=workflow_state.repository_url,
                action="workflow_stop",
                result="success",
                details={"workflow_id": workflow_id}
            ))
            
            return {
                "success": True,
                "workflow_id": workflow_id,
                "status": workflow_state.status.value
            }
            
        except Exception as e:
            logger.error(f"Failed to stop workflow {workflow_id}: {e}")
            return {
                "success": False,
                "workflow_id": workflow_id,
                "error": str(e)
            }
    
    def cleanup_resources(self):
        """Cleanup resources and close connections"""
        self.ssh_manager.close_all_connections()
        logger.info("Orchestrator resources cleaned up")

# CLI interface
if __name__ == "__main__":
    import argparse
    
    async def main():
        parser = argparse.ArgumentParser(description="Enhanced Two-Tier Orchestrator")
        parser.add_argument("--repository", required=True, help="Repository URL")
        parser.add_argument("--task-prompt", help="Task prompt for traditional workflow")
        parser.add_argument("--target-host", help="Target deployment host")
        parser.add_argument("--list-workflows", action="store_true", help="List active workflows")
        parser.add_argument("--status", help="Get workflow status by ID")
        parser.add_argument("--stop", help="Stop workflow by ID")
        
        args = parser.parse_args()
        
        orchestrator = EnhancedTwoTierOrchestrator()
        
        try:
            if args.list_workflows:
                workflows = await orchestrator.list_active_workflows()
                print(json.dumps(workflows, indent=2))
            elif args.status:
                status = await orchestrator.get_workflow_status(args.status)
                print(json.dumps(status, indent=2, default=str))
            elif args.stop:
                result = await orchestrator.stop_workflow(args.stop)
                print(json.dumps(result, indent=2))
            else:
                result = await orchestrator.run_unified_workflow(
                    repository_url=args.repository,
                    task_prompt=args.task_prompt,
                    target_host=args.target_host
                )
                print(json.dumps(result, indent=2, default=str))
        
        finally:
            orchestrator.cleanup_resources()
    
    asyncio.run(main())
