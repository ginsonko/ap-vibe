"""Register AP-Vibe in DSH's real profile patch layer without rewriting YAML.

DSH Desktop bundles dsh-app-boot: profiles/<name>/cordis.patch.yml is a
top-level patch sequence, and adding a plugin requires {"insert": [entry]}.
settings.yaml is a different settings namespace, not a plugin registry.
PyYAML compose reads syntax nodes only; !!js expressions are never evaluated.
"""
import hashlib
import json
from pathlib import Path
import uuid

BEGIN = "# AP-VIBE-MCP BEGIN"
END = "# AP-VIBE-MCP END"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _identity_present(node, visited=None):
    import yaml
    visited = set() if visited is None else visited
    if id(node) in visited:
        return False
    visited.add(id(node))
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if (isinstance(key, yaml.ScalarNode) and isinstance(value, yaml.ScalarNode)
                    and key.value in {"id", "serverName"} and value.value in {"mcp-ap-vibe", "ap-vibe"}):
                return True
            if _identity_present(value, visited):
                return True
    elif isinstance(node, yaml.SequenceNode):
        return any(_identity_present(item, visited) for item in node.value)
    return False


def patch_document(text, plugin, known_hashes=()):
    import yaml
    # Syntax composition preserves unknown custom tags and all untouched bytes.
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError("dsh_patch_yaml_invalid") from exc
    if node is not None and not isinstance(node, yaml.SequenceNode):
        raise ValueError("dsh_patch_must_be_sequence")
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError("dsh_managed_block_invalid")
    newline = "\r\n" if "\r\n" in text else "\n"
    item = {"insert":[plugin]}
    if BEGIN in text:
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        old = text[start:end]
        if digest(old.encode("utf-8")) not in known_hashes:
            raise ValueError("dsh_managed_block_user_modified")
        inner = old.splitlines()[1:-1]
        flow = bool(node and node.flow_style)
        separator = "," if flow and any(line.lstrip().startswith(",") for line in inner) else ""
    else:
        if node is not None and _identity_present(node):
            return text, None, "existing_user_registration"
        flow = bool(node and node.flow_style and node.value)
        separator = ""
        if flow:
            start = end = node.end_mark.index - 1
            trailing = text[node.value[-1].end_mark.index:start]
            uncommented = "\n".join(line.split("#",1)[0] for line in trailing.splitlines())
            separator = "" if "," in uncommented else ","
        elif node is not None and node.flow_style:  # Empty []: replace only the node.
            start, end = node.start_mark.index, node.end_mark.index
        else:
            start = end = node.end_mark.index if node else len(text)
    body = (separator + json.dumps(item, ensure_ascii=False) if flow else
            yaml.safe_dump([item], allow_unicode=True, sort_keys=False).rstrip().replace("\n",newline))
    block = BEGIN + newline + body + newline + END
    left, right = text[:start], text[end:]
    output = left + (newline if left and not left.endswith(("\n","\r")) else "") + block
    output += (newline if not right.startswith(("\n","\r")) else "") + right
    try:
        yaml.compose(output, Loader=yaml.SafeLoader)  # Validate splice before writing.
    except yaml.YAMLError as exc:
        raise ValueError("dsh_patch_splice_invalid") from exc
    return output, digest(block.encode("utf-8")), "managed"


def install_profiles(home, config_path, plugin, atomic):
    """Preserve user edits; return per-profile status and exact changed paths."""
    home, config_path = Path(home).resolve(), Path(config_path)
    results, changed = [], []
    for profile in sorted((home/"profiles").glob("*")):
        if not profile.is_dir() or profile.is_symlink() or not profile.resolve().is_relative_to(home):
            continue
        path = profile/"cordis.patch.yml"
        if not (profile/"package.json").is_file() or path.is_symlink():
            continue
        identity = digest(str(path.resolve()).encode())[:20]
        state_path = config_path.parent/"integrations"/("dsh-mcp-"+identity+".json")
        try:
            before = path.read_bytes() if path.exists() else b""
            state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"blocks":[]}
            if not isinstance(state, dict) or not isinstance(state.get("blocks"), list):
                raise ValueError("dsh_managed_state_invalid")
            original = before.decode("utf-8-sig")
            output, block_hash, ownership = patch_document(original, plugin, state.get("blocks",[]))
            if ownership == "existing_user_registration":
                results.append({"profile":profile.name,"status":ownership,"path":str(path)})
                continue
            after = (b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b"") + output.encode("utf-8")
            if after == before:
                results.append({"profile":profile.name,"status":"unchanged","path":str(path)})
                continue
            # Both generations remain accepted after an interrupted install.
            state["blocks"] = sorted(set(state.get("blocks",[]) + [block_hash]))
            atomic(state_path,json.dumps(state,indent=2).encode())
            if (path.read_bytes() if path.exists() else b"") != before:
                results.append({"profile":profile.name,"status":"concurrent_edit_preserved","path":str(path)})
                continue
            if before:
                atomic(state_path.parent/"backups"/(uuid.uuid4().hex+"-cordis.patch.yml"),before)
            # Check again after saving the backup: desktop live-edit may race.
            if (path.read_bytes() if path.exists() else b"") != before:
                results.append({"profile":profile.name,"status":"concurrent_edit_preserved","path":str(path)})
                continue
            atomic(path,after)
            changed.append(str(path))
            results.append({"profile":profile.name,"status":"installed","path":str(path)})
        except (ValueError, UnicodeError, OSError) as exc:
            results.append({"profile":profile.name,"status":"user_file_preserved","path":str(path),
                            "reason":str(exc) if str(exc).startswith("dsh_") else type(exc).__name__})
    return {"profiles":results,"changed":changed,
            "registered":any(x["status"] in {"installed","unchanged","existing_user_registration"} for x in results),
            "issues":any(x["status"] in {"user_file_preserved","concurrent_edit_preserved"} for x in results)}
