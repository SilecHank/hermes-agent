"""Pure guards for explicit SOP-bound input-volume requests (no retrieval)."""
import re


def sop_scope(question):
    documents = re.findall(r"(?<![A-Za-z0-9])SOP[- ]JL[- ]\d+", question, re.I)
    scopes = re.findall(r"(?<![A-Za-z0-9])(SOP[- ]JL[- ]\d+)\s+(A\d+|B\d+|C\d+)(?![0-9A-Za-z])", question, re.I)
    revisions = re.findall(r"(?<![A-Za-z0-9])(?:A|B|C)\d+(?![0-9A-Za-z])", question, re.I)
    if len(documents) != 1 or len(scopes) != 1 or len(revisions) != 1:
        return None
    return tuple(re.sub(r" +", "-", x.upper()) for x in scopes[0])


def explicit_sop_input_request(question):
    return bool(sop_scope(question) and re.search(r"多少|体积|用量|微升|毫升|[uµμm]L", question, re.I)
                and re.search(r"加入|投入|输入|提取|裂解", question)
                and not re.search(r"文件|路径|目录|清单|设备列表|试剂列表|时间|温度|浓度|循环", question))


def condition_asserted(question, term):
    """Require a literal scoped condition, rejecting denied/uncertain mentions."""
    compact = lambda text: re.sub(r"[\s-]+", "", text).casefold()
    text, token = compact(question), compact(term)
    if token not in text:
        return False
    # An additional conflicting identifier in the same controlled family is
    # not confirmation of the required platform/reagent.
    for pattern in (r"s\d+", r"md\d+"):
        required = re.findall(pattern, token)
        mentioned = set(re.findall(pattern, text))
        if required and mentioned != set(required):
            return False
    for clause in re.split(r"[，,。；;？?]", text):
        if token in clause and re.search(r"不是|并非|不用|未确认|不确定|是否|是不是|可能|或|不能确认|不采用|未采用|没有使用|未使用|不使用", clause):
            return False
    return True
