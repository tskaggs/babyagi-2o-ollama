# agents/manager.py
import time, re, json, os
from datetime import datetime

from agents.agent_service import AgentService
from agents.db import init_db, get_db
from agents.logging_utils import log_manager
from agents.manager_analytics import ManagerAnalytics
from agents.message_bus import MessageBus
from agents.orchestration_service import OrchestrationService

class Manager:
    def __init__(self, model_name, ollama, colors, agent_colors, agent_emojis, verbose=False):
        self.model_name = model_name
        self.ollama = ollama
        self.colors = colors
        self.agent_colors = agent_colors
        self.agent_emojis = agent_emojis
        self.bus = MessageBus()
        self.agents = []
        self.agent_names = []
        self.progress = {}
        self.completed = set()
        self.verbose = verbose

    def estimate_agents(self, main_task):
        """Use Ollama to estimate a list of subtasks/agents for the main task."""
        base_prompt = (
            "You are an expert project planner. Given a user task, break it down into 2-6 clear, actionable subtasks. "
            "Return only a JSON list of strings, each string being a subtask. Do not include any explanation, markdown, or extra text. "
            "Output ONLY a valid JSON array, e.g. [\"Subtask 1\", \"Subtask 2\"]"
        )
        for attempt in range(3):
            prompt = base_prompt
            if attempt > 0:
                prompt += "\n\nPrevious output was not valid JSON. Please output ONLY a valid JSON array, no extra text."
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": main_task}
            ]
            try:
                response = self.ollama.chat(model=self.model_name, messages=messages)
            except Exception as e:
                if 'hourly usage limit' in str(e) or 'status code: 402' in str(e):
                    log_manager("Error Ollama: you've reached your hourly usage limit, please upgrade to continue", colors=self.colors, level="ERROR")
                    exit(1)
                else:
                    raise
            # Support both dict and object response, fallback to str if needed
            if hasattr(response, 'message'):
                content = response.message
            elif isinstance(response, dict) and 'message' in response:
                content = response['message']
            else:
                content = str(response)
            # Ensure content is a string (handle pydantic Message or other objects)
            if not isinstance(content, str):
                content = str(getattr(content, 'content', content))
            try:
                agent_list = json.loads(content)
                if isinstance(agent_list, list) and all(isinstance(x, str) for x in agent_list):
                    return agent_list
            except Exception as e:
                log_manager(f"Attempt {attempt+1}: Could not parse JSON from LLM response. Error: {e}", colors=self.colors, level="WARNING")
            # Fallback: try to extract from numbered/bulleted list
            lines = content.splitlines()
            extracted = []
            for line in lines:
                m = re.match(r'\s*(?:\d+\.|[-*])\s+(.*)', line)
                if m:
                    item = m.group(1).strip().strip('"').strip("'")
                    if item:
                        extracted.append(item)
            # Final fallback: split into sentences if possible
            sentences = re.split(r'(?<=[.!?])\s+', content.strip())
            sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
            if len(sentences) > 1:
                log_manager(f"LLM did not return a list, but split into {len(sentences)} subtasks using sentences. Raw response was:\n{content}", colors=self.colors, level="WARNING")
                return sentences
        log_manager(f"Could not parse agent list from LLM after 3 attempts and all fallbacks.\nRaw LLM response was:\n{content}", colors=self.colors, level="ERROR")
        return [main_task]


    def assign_tasks(self, agent_list):
        manager_name = "manager"
        for idx, name in enumerate(self.agent_names):
            self.bus.send(manager_name, name, f"You are assigned the following minimal task: {agent_list[idx]}")


    def orchestrate(self):
        init_db()
        # Show two example tasks and allow user to select or enter their own
        example1 = "Scrape techmeme.com and summarize the top headlines."
        example2 = "Make a mini ai agent."
        log_manager("Describe the task you want to complete:", colors=self.colors, level="BOLD")
        log_manager(f"  1. {example1}")
        log_manager(f"  2. {example2}")
        log_manager("  3. Enter your own task")
        choice = input(f"{self.colors.OKBLUE}Select 1, 2, or type your own task:{self.colors.ENDC} ")
        if choice.strip() == '1':
            main_task = example1
        elif choice.strip() == '2':
            main_task = example2
        elif choice.strip() == '3' or not choice.strip():
            main_task = input(f"{self.colors.OKBLUE}Enter your custom task:{self.colors.ENDC} ")
        else:
            main_task = choice.strip()

        # Generate random project name
        import random
        cousins = ["shrimp", "lobster", "crab", "prawn", "copepod", "amphipod", "isopod", "mantis", "mysid", "barnacle"]
        colors = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "black", "white", "gray"]
        project_name = f"{random.choice(cousins)}-{random.choice(colors)}-{random.randint(1, 99)}"
        log_manager(f"Project name: {project_name}", colors=self.colors, level="INFO")
        self.project_name = project_name
        log_manager("Manager is analyzing the main task and creating minimal subtasks...", colors=self.colors, level="INFO")
        agent_list = self.estimate_agents(main_task)
        log_manager(f"Manager created {len(agent_list)} minimal subtasks:", colors=self.colors, level="SUCCESS")
        for i, subtask in enumerate(agent_list):
            log_manager(f"  {i+1}. {subtask}")

        # Prompt for number of agents (default 1)
        while True:
            num_agents = input(f"{self.colors.OKBLUE}How many agents do you want to use? (1-infinite, default 1): {self.colors.ENDC}")
            if not num_agents.strip():
                num_agents = 1
                break
            try:
                num_agents = int(num_agents)
                if num_agents >= 1:
                    break
            except ValueError:
                print(f"{self.colors.WARNING}Please enter a valid integer greater than or equal to 1.{self.colors.ENDC}")

        # Assign subtasks to agents
        if num_agents == 1:
            agent_names = ["agent_1"]
            agent_subtasks = [agent_list]
        else:
            agent_names = [f"agent_{i+1}" for i in range(num_agents)]
            agent_subtasks = [[] for _ in range(num_agents)]
            for idx, subtask in enumerate(agent_list):
                agent_subtasks[idx % num_agents].append(subtask)

        log_manager("\nAgent Assignments:", colors=self.colors, level="BOLD")
        for idx, name in enumerate(agent_names):
            emoji = self.agent_emojis[idx % len(self.agent_emojis)]
            # Show all subtasks for each agent
            if num_agents == 1:
                for j, subtask in enumerate(agent_list):
                    log_manager(f"  {emoji} {name}: {subtask}")
            else:
                for j, subtask in enumerate(agent_subtasks[idx]):
                    log_manager(f"  {emoji} {name} subtask {j+1}: {subtask}")

        # Prompt for number of iterations (default 1)
        while True:
            num_iterations = input(f"{self.colors.OKBLUE}How many iterations per agent? (1-infinite, default 1): {self.colors.ENDC}")
            if not num_iterations.strip():
                num_iterations = 1
                break
            try:
                num_iterations = int(num_iterations)
                if num_iterations >= 1:
                    break
            except Exception:
                pass
            log_manager("Please enter a valid integer >= 1 or leave blank for 1.", colors=self.colors, level="WARNING")

        self.num_agents = num_agents
        self.num_iterations = num_iterations

        # Assign subtasks to agents
        if self.num_agents == 1:
            agent_names = ["agent_1"]
            agent_subtasks = [agent_list]
        else:
            agent_names = [f"agent_{i+1}" for i in range(self.num_agents)]
            agent_subtasks = [[] for _ in range(self.num_agents)]
            for idx, subtask in enumerate(agent_list):
                agent_subtasks[idx % self.num_agents].append(subtask)

        # Save run and agent assignments to DB
        with get_db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO runs (task, manager_subtasks) VALUES (?, ?)", (main_task, json.dumps(agent_list)))
            run_id = c.lastrowid
            agent_ids = {}
            for idx, agent_name in enumerate(agent_names):
                c.execute("INSERT INTO agents (run_id, agent_name, assigned_subtask) VALUES (?, ?, ?)", (run_id, agent_name, json.dumps(agent_subtasks[idx])))
                agent_ids[agent_name] = c.lastrowid
            conn.commit()
        # Use AgentService for agent creation
        agent_service = AgentService(
            agent_colors=self.agent_colors,
            agent_emojis=self.agent_emojis,
            model_name=self.model_name,
            ollama=self.ollama,
            colors=self.colors,
            bus=self.bus,
            verbose=self.verbose,
            num_iterations=self.num_iterations
        )
        self.agent_names, self.agents = agent_service.create_agents(agent_subtasks)
        self.progress = {name: None for name in self.agent_names}
        self.completed = set()
        start_time = time.time()
        token_count = 0
        # Store for later DB updates
        self._db_run_id = run_id
        self._db_agent_ids = agent_ids
        # Show agent assignments
        log_manager("\nAgent Assignments:", colors=self.colors, level="BOLD")
        for idx, name in enumerate(self.agent_names):
            emoji = self.agent_emojis[idx % len(self.agent_emojis)]
            log_manager(f"  {emoji} {name}: {agent_list[idx]}")

        # Use OrchestrationService for main review/approval loop
        orchestration_service = OrchestrationService(
            bus=self.bus,
            agent_names=self.agent_names,
            db_run_id=self._db_run_id,
            db_agent_ids=self._db_agent_ids,
            colors=self.colors,
            agent_emojis=self.agent_emojis
        )
        token_count_box = [token_count]  # mutable box for token_count
        agent_task_progress, agent_task_summaries = orchestration_service.run_orchestration(
            num_iterations=self.num_iterations,
            get_agent_tasks=self._get_agent_tasks,
            progress=self.progress,
            completed=self.completed,
            _get_db=get_db,
            token_count=token_count_box
        )

        # Summarize and log run using ManagerAnalytics
        analytics = ManagerAnalytics(get_db, self.colors)
        analytics.save_run_summary(
            run_id=self._db_run_id,
            agent_names=self.agent_names,
            progress=self.progress,
            start_time=start_time,
            token_count=token_count_box[0]
        )

        # --- Save summary and solution to complete/project_name directory ---
        complete_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'complete', self.project_name)
        os.makedirs(complete_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        # Compile agent files into final report
        agent_files = []
        for name in self.agent_names:
            agent_dir = os.path.join(complete_dir, name)
            if os.path.exists(agent_dir):
                for fname in os.listdir(agent_dir):
                    agent_files.append(os.path.join(agent_dir, fname))
        report_lines = [
            f"Project: {self.project_name}",
            f"Task: {main_task}",
            f"Completed at: {timestamp}",
            "",
            "Agent Summaries:",
        ]
        for name in self.agent_names:
            report_lines.append(f"- {name}: {self.progress.get(name, '')}")
        report_lines.append("\nAgent Files:")
        for fpath in agent_files:
            report_lines.append(f"  {os.path.basename(fpath)}")
        report_lines.append("\nAgent Task Details:")
        for name, summaries in agent_task_summaries.items():
            report_lines.append(f"\n{name}:")
            for s in summaries:
                report_lines.append(f"  {s}")
        report_path = os.path.join(complete_dir, f"report_{timestamp}.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        log_manager(f"\n{self.colors.OKGREEN}Final report saved to {report_path}{self.colors.ENDC}", colors=self.colors, level="SUCCESS")


    def _get_agent_tasks(self, name):
        # Helper to get the list of tasks assigned to an agent from the DB
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT assigned_subtask FROM agents WHERE agent_name=? ORDER BY id DESC LIMIT 1", (name,))
            row = c.fetchone()
            if row:
                try:
                    return row[0]
                except Exception:
                    return "[]"
            return "[]"
