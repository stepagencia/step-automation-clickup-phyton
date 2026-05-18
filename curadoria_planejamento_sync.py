import os
import re
import requests
from datetime import datetime

API_TOKEN = os.environ.get("CLICKUP_API_TOKEN")
HEADERS = {
    "Authorization": API_TOKEN,
    "Content-Type": "application/json"
}

CURADORIA_LIST_ID = "901327106936"
PLANEJAMENTO_LIST_ID = "901306281641"

STATUS_ACEITA = "ACEITA"
STATUS_DESTINO = "próximos planejamentos"

STATUS_ELEGIVEIS = {
    "próximos planejamentos",
    "subir planejamento",
    "planejando/fazendo"
}

MESES = {
    "Janeiro": 1, "Fevereiro": 2, "Março": 3, "Abril": 4,
    "Maio": 5, "Junho": 6, "Julho": 7, "Agosto": 8,
    "Setembro": 9, "Outubro": 10, "Novembro": 11, "Dezembro": 12
}


def get_tasks_from_list(list_id):
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    tasks = []
    page = 0
    while True:
        params = {"page": page, "subtasks": "false", "include_closed": "false"}
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("tasks", [])
        tasks.extend(batch)
        if data.get("last_page", True):
            break
        page += 1
    return tasks


def get_cliente_field(task):
    """Lê o valor do campo dropdown 'Cliente' da task."""
    for field in task.get("custom_fields", []):
        if field.get("name", "").strip().lower() == "cliente":
            value = field.get("value")
            if value is None:
                return None
            options = field.get("type_config", {}).get("options", [])
            for opt in options:
                # ClickUp retorna orderindex como valor do dropdown
                if str(opt.get("orderindex")) == str(value):
                    return opt.get("name", "").strip()
            return None
    return None


def extract_date_from_name(name):
    """Extrai datetime de '[Planejamento de Conteúdo] [Nome] [Mês/Ano]'."""
    match = re.search(r'\[(\w+)/(\d{4})\]', name)
    if not match:
        return None
    month_str = match.group(1).capitalize()
    year = int(match.group(2))
    month = MESES.get(month_str)
    if not month:
        return None
    return datetime(year, month, 1)


def find_target_planning(client_name, planning_tasks):
    """
    Entre os planejamentos do cliente com status elegível,
    retorna o mais próximo cronologicamente.
    """
    candidates = []
    for task in planning_tasks:
        name = task.get("name", "")
        status = task.get("status", {}).get("status", "").strip().lower()

        if client_name.lower() not in name.lower():
            continue
        if status not in STATUS_ELEGIVEIS:
            continue

        date = extract_date_from_name(name)
        if date:
            candidates.append((date, task))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def set_parent(task_id, parent_id):
    """Define a task como subtarefa do planejamento alvo."""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    resp = requests.put(url, headers=HEADERS, json={"parent": parent_id})
    resp.raise_for_status()


def update_status(task_id, status):
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    resp = requests.put(url, headers=HEADERS, json={"status": status})
    resp.raise_for_status()


def main():
    print("=== Curadoria → Planejamento Sync ===\n")

    print("Buscando tasks ACEITAS na Curadoria...")
    curadoria_tasks = get_tasks_from_list(CURADORIA_LIST_ID)
    aceitas = [
        t for t in curadoria_tasks
        if t.get("status", {}).get("status", "").strip().upper() == STATUS_ACEITA
    ]
    print(f"Tasks ACEITAS encontradas: {len(aceitas)}\n")

    if not aceitas:
        print("Nada a processar.")
        return

    print("Buscando planejamentos...")
    planning_tasks = get_tasks_from_list(PLANEJAMENTO_LIST_ID)
    print(f"Planejamentos encontrados: {len(planning_tasks)}\n")

    for task in aceitas:
        task_id = task["id"]
        task_name = task.get("name", "—")

        # Evita reprocessar tasks que já têm parent
        if task.get("parent"):
            print(f"[SKIP] '{task_name}' — já tem planejamento pai.")
            continue

        client_name = get_cliente_field(task)
        if not client_name:
            print(f"[SKIP] '{task_name}' — campo Cliente vazio.")
            continue

        print(f"Processando: '{task_name}' | Cliente: {client_name}")

        target = find_target_planning(client_name, planning_tasks)
        if not target:
            print(f"  → Nenhum planejamento elegível para '{client_name}'. Pulando.\n")
            continue

        print(f"  → Planejamento alvo: '{target['name']}'")

        set_parent(task_id, target["id"])
        update_status(task_id, STATUS_DESTINO)

        print(f"  → Subtarefa criada + status atualizado para '{STATUS_DESTINO}'\n")

    print("=== Concluído ===")


if __name__ == "__main__":
    main()
