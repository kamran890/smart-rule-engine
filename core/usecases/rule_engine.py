from core.services.ai import AIClient
from core.config.rule_engine import system_prompt, system_data, expected_rule_chain
from core.utils.json import get_valid_json
from core.usecases.iot_platform import UpdateDeviceAttributeUsecase
from core.entities.rule_engine import (
    NodeEntity,
    RuleChainEntity,
    NodeTypes,
    RuleChainGenerateEntity,
)
from core.db_repos.rule_engine import RuleChainRepo
from core.db_repos.iot_platform import DeviceDataRepo

from typing import Dict, Any, Optional, List

from py_mini_racer import py_mini_racer


class ListRuleChainUseCase:
    def __init__(self, rule_chain_repo: RuleChainRepo) -> None:
        self.rule_chain_repo = rule_chain_repo
        self.rule_chain_entities: Optional[List[RuleChainEntity]] = []

    def execute(self) -> List[dict]:
        self.rule_chain_entities = self.rule_chain_repo.get_all_entries()
        return [entity.to_dict() for entity in self.rule_chain_entities]


class RetrieveRuleChainUseCase:
    def __init__(self, rule_chain_repo: RuleChainRepo) -> None:
        self.rule_chain_repo = rule_chain_repo
        self.rule_chain_entity: Optional[RuleChainEntity] = None
        self.rule_chain_id: Optional[int] = None

    def set_params(self, rule_chain_id: int) -> None:
        self.rule_chain_id = rule_chain_id

    def execute(self) -> dict:
        self.rule_chain_entity = self.rule_chain_repo.get_by_id(
            id=self.rule_chain_id
        )
        return self.rule_chain_entity.to_dict()


class DeleteRuleChainUseCase:
    def __init__(self, rule_chain_repo: RuleChainRepo) -> None:
        self.rule_chain_repo = rule_chain_repo
        self.rule_chain_id: Optional[int] = None

    def set_params(self, rule_chain_id: int) -> None:
        self.rule_chain_id = rule_chain_id

    def execute(self) -> None:
        self.rule_chain_repo.delete_by_id(id=self.rule_chain_id)


class BulkDeleteRuleChainUseCase:
    def __init__(self, rule_chain_repo: RuleChainRepo) -> None:
        self.rule_chain_repo = rule_chain_repo
        self.rule_chain_ids: Optional[List[int]] = None

    def set_params(self, rule_chain_ids: List[int]) -> None:
        self.rule_chain_ids = rule_chain_ids

    def execute(self) -> None:
        for _id in self.rule_chain_ids:
            self.rule_chain_repo.delete_by_id(id=_id)


class GenerateRuleChainUseCase:
    def __init__(
        self,
        ai_client: AIClient,
        rule_chain_repo: RuleChainRepo,
        device_data_repo: DeviceDataRepo
    ):
        self.ai_client = ai_client
        self.rule_chain_repo = rule_chain_repo
        self.device_data_repo = device_data_repo
        self.rule_chain_entity = None

    def set_params(self, data):
        self.rule_chain_generate_entity = RuleChainGenerateEntity(**data)

    def execute(self):
        messages = [
            {
                "role": "system",
                "content": f"{system_prompt}{self.get_system_data()}{expected_rule_chain}"
            }
        ] + self.rule_chain_generate_entity.chat_history + [
            {
                "role": "user",
                "content": self.rule_chain_generate_entity.user_prompt
            }
        ]

        response = self.ai_client.send_prompt(
            messages=messages
        )

        try:
            self.rule_chain_generate_entity.is_generated = True
            response_data = get_valid_json(response)
            response_data["integration_id"] = self.rule_chain_generate_entity.integration_id
            self.rule_chain_entity = RuleChainEntity.from_dict(response_data)

            self.save_rule_chain()

            self.rule_chain_generate_entity.chat_history = messages + [
                {
                    "role": "assistant",
                    "content": f"Rule Chain is generated successfully with name {self.rule_chain_entity.name} Refer to it with this name if need to change anything or delete this rule chain. You can also list all the rule chains currently in system by asking me about it."
                }
            ]
        except Exception as ex:
            print(ex)
            self.rule_chain_generate_entity.is_generated = False
            self.rule_chain_generate_entity.chat_history = messages + [
                {
                    "role": "assistant",
                    "content": response
                }
            ]
        self.rule_chain_generate_entity.chat_history = [
            entry for entry in self.rule_chain_generate_entity.chat_history
            if entry["role"] != "system"
        ]
        return self.rule_chain_generate_entity.to_dict()


    def save_rule_chain(self):
        self.rule_chain_repo.create(
            **self.rule_chain_entity.to_dict(exclude_fields=['id'])
        )

    def get_system_data(self):
        device_data_entities = self.device_data_repo.get_by_integration_id(
            integration_id=self.rule_chain_generate_entity.integration_id
        )
        return [
            entity.to_dict(exclude_fields=['id', 'integration_id'])
            for entity in device_data_entities
        ]


class RuleChainExecutorUsecase:
    def __init__(self, update_device_attribute_usecase: UpdateDeviceAttributeUsecase) -> None:
        self.update_device_attribute_usecase = update_device_attribute_usecase

    def set_params(self, rule_chain_data: list, context: Dict[str, Any]):
        self.rule_chain_entity = RuleChainEntity.from_dict(rule_chain_data)
        self.nodes = {str(node.id): node for node in self.rule_chain_entity.nodes}
        self.context = context
        self.intermediate_context = {}

    def execute(self) -> dict:
        current_node_id = next(
            node_id for node_id, node in self.nodes.items()
            if node.type == NodeTypes.SOURCE_NODE
        )
        while current_node_id:
            node = self.nodes[current_node_id]
            current_node_id = self.execute_node(node)
        return self.context

    def execute_node(self, node: NodeEntity) -> Optional[str]:
        if node.type == NodeTypes.SOURCE_NODE:
            return self.execute_source_node(node)
        elif node.type == NodeTypes.SCRIPT_NODE:
            return self.execute_script_node(node)
        elif node.type == NodeTypes.SWITCH_NODE:
            return self.execute_switch_node(node)
        elif node.type == NodeTypes.ACTION_NODE:
            return self.execute_action_node(node)
        else:
            raise ValueError(f"Unknown node type: {node.type}")

    def execute_source_node(
        self,
        node: NodeEntity,
    ) -> Optional[str]:
        device_id = node.config.device_id
        parameter_id = node.config.parameter_id

        try:
            self.intermediate_context["input"] = (
                self.context["devices"][device_id][parameter_id]
            )
        except KeyError:
            return None

        return str(node.target_node_id)

    def execute_script_node(
        self,
        node: NodeEntity,
    ) -> str:
        ctx = py_mini_racer.MiniRacer()
        ctx.eval(node.config.script)
        result = ctx.call("executeScript", self.intermediate_context["input"])

        self.intermediate_context["input"] = result
        return str(node.target_node_id)

    def execute_switch_node(
        self,
        node: NodeEntity,
    ) -> Optional[str]:
        _input = self.intermediate_context["input"]
        for index, condition in enumerate(node.config):
            if condition.condition == "==" and condition.value == _input:
                return str(node.target_node_id[index])
        return None

    def execute_action_node(self, node: NodeEntity) -> None:
        for action in node.config:
            device = self.context["devices"].setdefault(action.device_id, {})
            device[action.parameter_id] = action.value
            self.perform_action(
                device_id=action.device_id,
                key=action.parameter_id,
                value=action.value
            )
        return None

    def perform_action(self, device_id, key, value) -> None:
        self.update_device_attribute_usecase.set_params(
            integration_id=self.rule_chain_entity.integration_id,
            device_id=device_id,
            key=key,
            value=value
        )
        self.update_device_attribute_usecase.execute()


class AllRuleChainsExecutorUsecase:
    def __init__(
        self,
        rule_chain_repo: RuleChainRepo,
        rule_chain_executor_usecase: GenerateRuleChainUseCase,
    ) -> None:
        self.rule_chain_executor_usecase = rule_chain_executor_usecase
        self.rule_chain_repo = rule_chain_repo

    def set_params(self, context: Dict[str, Any]):
        self.context = context

    def execute(self) -> List[Dict]:
        response_data = []

        rule_chain_entities = self.rule_chain_repo.get_all_entries()
        for rule_chain_entity in rule_chain_entities:
            self.rule_chain_executor_usecase.set_params(
                rule_chain_data=rule_chain_entity.to_dict(),
                context=self.context
            )
            result = self.rule_chain_executor_usecase.execute()
            response_data.append(result)
        return self.context
