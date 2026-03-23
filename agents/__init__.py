import os

from google.adk.agents import SequentialAgent, LlmAgent
from google.adk.models.lite_llm import LiteLlm

# LiteLlm routes gemini/ models to Google AI Studio using GEMINI_API_KEY specifically.
# If only GOOGLE_API_KEY is set, copy it so LiteLlm can find it.
if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]

from tools.decompose_tools import assess_complexity, decompose_workflow, estimate_activity_count
from tools.retrieval_tools import retrieve_all_steps, load_activity_list
from tools.pattern_tools import load_pattern_library, match_pattern, score_pattern_match, get_examples_for_control_flow
from tools.build_tools import load_activity_template, resolve_control_flow, build_activity_json, fill_scaffold_params, generate_pnumber, generate_workflow_name
from tools.annotation_tools import inject_unavailable_stubs, annotate_placeholders, add_verify_notes, collect_placeholder_summary
from tools.validation_tools import validate_xname_uniqueness, validate_activity_schema, validate_control_flow_rules, validate_required_fields, run_all_validators
from tools.compose_tools import serialize_to_xml, write_output_file, format_chat_response
from tools.xml_validation_tools import validate_xml_output

from agents.decomposer_agent import INSTRUCTION as DECOMPOSER_INSTRUCTION
from agents.pattern_matcher_agent import INSTRUCTION as PATTERN_INSTRUCTION
from agents.retriever_agent import INSTRUCTION as RETRIEVER_INSTRUCTION
from agents.structure_builder_agent import INSTRUCTION as STRUCTURE_INSTRUCTION
from agents.annotation_agent import INSTRUCTION as ANNOTATION_INSTRUCTION
from agents.validation_agent import INSTRUCTION as VALIDATION_INSTRUCTION
from agents.composer_agent import INSTRUCTION as COMPOSER_INSTRUCTION


def _model():
    """
    StructureBuilderAgent — LiteLlm with Gemini 2.5 Pro.
    Native Gemini() class causes 400 errors on tool schema serialization
    (additional_properties=null ADK bug). Using LiteLlm until ADK fixes this.
    Pro chosen for stronger instruction following on complex assembly tasks.
    api_key passed explicitly to force Google AI Studio routing.
    """
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        max_tokens=8192,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def _model_fast():
    """
    All other agents — LiteLlm with Gemini 2.5 Flash.
    api_key passed explicitly to force Google AI Studio routing.
    """
    return LiteLlm(
        model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def build_pipeline() -> SequentialAgent:
    # Always create fresh LlmAgent instances — ADK raises validation error
    # if an agent already has a parent (singleton reuse pattern is broken).
    return SequentialAgent(
        name="WorkflowGeneratorPipeline",
        description=(
            "Pattern-first pipeline: matches intent to confirmed workflow patterns, "
            "then fills parameters. Outputs importable Resolve Actions XML."
        ),
        sub_agents=[
            LlmAgent(
                name="DecomposerAgent",
                model=_model_fast(),
                instruction=DECOMPOSER_INSTRUCTION,
                tools=[assess_complexity, decompose_workflow, estimate_activity_count],
                output_key="decomposition",
                include_contents="none",
            ),
            LlmAgent(
                name="PatternMatcherAgent",
                model=_model_fast(),
                instruction=PATTERN_INSTRUCTION,
                tools=[load_pattern_library, match_pattern, score_pattern_match],
                output_key="pattern_match",
                include_contents="none",
            ),
            LlmAgent(
                name="ActivityRetrieverAgent",
                model=_model_fast(),
                instruction=RETRIEVER_INSTRUCTION,
                tools=[load_activity_list, retrieve_all_steps],
                output_key="activity_manifest",
                include_contents="none",
            ),
            LlmAgent(
                name="StructureBuilderAgent",
                model=_model(),
                instruction=STRUCTURE_INSTRUCTION,
                tools=[
                    load_activity_template, resolve_control_flow,
                    build_activity_json, fill_scaffold_params,
                    get_examples_for_control_flow,
                ],
                output_key="workflow_json",
                include_contents="none",
            ),
            LlmAgent(
                name="AnnotationAgent",
                model=_model_fast(),
                instruction=ANNOTATION_INSTRUCTION,
                tools=[
                    inject_unavailable_stubs, annotate_placeholders,
                    add_verify_notes, collect_placeholder_summary,
                ],
                output_key="annotation_result",
                include_contents="none",
            ),
            LlmAgent(
                name="ValidationAgent",
                model=_model_fast(),
                instruction=VALIDATION_INSTRUCTION,
                tools=[
                    validate_xname_uniqueness, validate_activity_schema,
                    validate_control_flow_rules, validate_required_fields,
                    run_all_validators,
                ],
                output_key="validation_result",
                include_contents="none",
            ),
            LlmAgent(
                name="ComposerAgent",
                model=_model_fast(),
                instruction=COMPOSER_INSTRUCTION,
                tools=[
                    serialize_to_xml, write_output_file, format_chat_response,
                    generate_pnumber, generate_workflow_name,
                    validate_xml_output,
                ],
                output_key="composer_result",
                include_contents="none",
            ),
        ],
    )
    