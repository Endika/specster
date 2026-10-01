"""Every fixed text Specster writes, in each comment language."""

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "questions": "Before I write the spec, I need a few answers",
        "hidden": "Hidden content removed",
        "untrusted": "Comments ignored by the trust filter",
        "objective": "Objective",
        "in_scope": "In scope",
        "out_of_scope": "Out of scope",
        "files": "Files",
        "approach": "Approach",
        "risks": "Risks",
        "tests": "Test strategy",
        "plan": "Plan",
        "task": "Task",
        "depends": "Depends on",
        "level": "Level",
        "reordered": "Specster reordered",
        "acceptance": "Acceptance criteria",
        "changes": "Changes from the previous spec",
        "evidence": "Evidence (requests run before and after the change)",
        "evidence_request": "Request",
        "evidence_why": "Shows",
        "evidence_pr": "Before and after",
        "evidence_files": "Full responses and server logs: {url}",
        "evidence_cut": "`{name}` {side} response cut to 64 KB",
        "evidence_upload_failed": "The evidence files could not be uploaded: {why}",
        "evidence_link_failed": (
            "The evidence files are on `specster-evidence` but the pull request could not be "
            "updated with the link: {why}"
        ),
        "evidence_changed": "Changed",
        "evidence_yes": "yes",
        "evidence_no": "no",
        "error": "Specster could not finish this run",
        "fix": "How to fix it",
        "budget": "Budget for this issue is spent",
        "next_questions": "Answer below, then add the `{label}` label again.",
        "next_spec": "Review the spec. The `{label}` label will build it.",
        "refused": "Specster will not build this issue",
        "pr_opened": "Pull request opened",
        "build_failed": "The build failed",
        "not_approved": "The reviewer did not approve the build",
        "build_budget": "The build stopped: its budget is spent",
        "build_time": "The build stopped: it reached its time limit (`build.max_minutes`)",
        "test_result": "Tests",
        "commit": "Commit",
        "status": "Status",
        "no_tests": "No tests were run: `build.test_command` is not set.",
        "tests_ok": "Tests pass on the branch ({runs} test runs in total).",
        "tests_bad": "Tests fail on the branch (exit {code}).",
        "tests_timeout": "Tests timed out on the branch.",
        "task_tests_bad": "Last test run of `{task}` failed (exit {code}).",
        "task_tests_timeout": "Last test run of `{task}` timed out.",
        "minor": "Minor findings",
        "pending": "Findings still open",
        "unapplied": "Comments after the spec, not applied: add `{label}` again to fold them in",
        "branch": "Branch",
        "no_branch": "Nothing was committed, so no branch was pushed.",
        "spec_link": "Spec",
        "next_pr": "Review the pull request.",
        "next_human": "A person needs to take it from here.",
        "st_done": "done",
        "st_failed": "failed",
        "st_skipped": "skipped",
        "escalated": "escalated to {model}",
        "st_not_started": "not started",
        "hint_default_branch": "Run the build from the default branch.",
        "hint_branch_exists": (
            "Delete the branch (or merge its pull request), then add the `{label}` label "
            "again. Specster never overwrites a branch."
        ),
        "hint_root": (
            "Run the build phase with the Docker action (it starts as root); see docs/build.md."
        ),
        "hint_pull_403": (
            "The branch was pushed. With GITHUB_TOKEN, enable 'Allow GitHub Actions to "
            "create and approve pull requests' in the repository settings, or pass a "
            "GitHub App token."
        ),
        "hint_pull_other": (
            "The branch was pushed, but GitHub refused the pull request (HTTP {status}). "
            "Open it from the branch by hand, or fix the cause, delete the branch and add "
            "the `{label}` label again."
        ),
        "hint_sandbox": (
            "Nothing was pushed. See the workflow log, fix the cause, then add the "
            "`{label}` label again."
        ),
        "hint_push": (
            "Check that the token has contents: write and that the branch does not exist "
            "on the remote, then add the `{label}` label again."
        ),
        "hint_role_model": "Check the provider credentials and the model id in {key}.",
        "hint_checkout": "Add actions/checkout before Specster.",
        "hint_docker_socket": (
            "Nothing ran. Remove the Docker socket mount or restrict its mode, then add the "
            "`{label}` label again."
        ),
        "hint_head": (
            "Check out the default branch at the commit the event saw (actions/checkout "
            "without a ref), then add the `{label}` label again."
        ),
    },
    "es": {
        "questions": "Antes de escribir la spec necesito algunas respuestas",
        "hidden": "Contenido oculto eliminado",
        "untrusted": "Comentarios ignorados por el filtro de confianza",
        "objective": "Objetivo",
        "in_scope": "Dentro del alcance",
        "out_of_scope": "Fuera del alcance",
        "files": "Ficheros",
        "approach": "Enfoque",
        "risks": "Riesgos",
        "tests": "Estrategia de tests",
        "plan": "Plan",
        "task": "Tarea",
        "depends": "Depende de",
        "level": "Nivel",
        "reordered": "Specster ha reordenado",
        "acceptance": "Criterios de aceptaci\u00f3n",
        "changes": "Cambios respecto a la spec anterior",
        "evidence": "Evidencia (peticiones antes y despu\u00e9s del cambio)",
        "evidence_request": "Petici\u00f3n",
        "evidence_why": "Muestra",
        "evidence_pr": "Antes y despu\u00e9s",
        "evidence_files": "Respuestas completas y logs del servidor: {url}",
        "evidence_cut": "Respuesta {side} de `{name}` cortada a 64 KB",
        "evidence_upload_failed": "No se pudieron subir los ficheros de evidencia: {why}",
        "evidence_link_failed": (
            "Los ficheros de evidencia est\u00e1n en `specster-evidence`, pero no se pudo "
            "a\u00f1adir el enlace a la pull request: {why}"
        ),
        "evidence_changed": "Cambia",
        "evidence_yes": "s\u00ed",
        "evidence_no": "no",
        "error": "Specster no ha podido terminar esta ejecuci\u00f3n",
        "fix": "C\u00f3mo arreglarlo",
        "budget": "El presupuesto de esta issue est\u00e1 agotado",
        "next_questions": "Responde abajo y vuelve a poner la etiqueta `{label}`.",
        "next_spec": "Revisa la spec. La etiqueta `{label}` la construir\u00e1.",
        "refused": "Specster no va a construir esta issue",
        "pr_opened": "Pull request abierta",
        "build_failed": "La construcci\u00f3n ha fallado",
        "not_approved": "El revisor no ha aprobado la construcci\u00f3n",
        "build_budget": "La construcci\u00f3n se ha parado: presupuesto agotado",
        "build_time": (
            "La construcci\u00f3n se ha parado: ha llegado a su tiempo m\u00e1ximo "
            "(`build.max_minutes`)"
        ),
        "test_result": "Tests",
        "commit": "Commit",
        "status": "Estado",
        "no_tests": "Sin tests ejecutados: `build.test_command` no est\u00e1 configurado.",
        "tests_ok": "Los tests pasan en la rama ({runs} ejecuciones en total).",
        "tests_bad": "Los tests fallan en la rama (salida {code}).",
        "tests_timeout": "Los tests han agotado el tiempo en la rama.",
        "task_tests_bad": "La última ejecución de tests de `{task}` ha fallado (salida {code}).",
        "task_tests_timeout": "La última ejecución de tests de `{task}` ha agotado el tiempo.",
        "minor": "Hallazgos menores",
        "pending": "Hallazgos pendientes",
        "unapplied": (
            "Comentarios posteriores a la spec, no aplicados: vuelve a "
            "poner `{label}` para incorporarlos"
        ),
        "branch": "Rama",
        "no_branch": "No hay ning\u00fan commit, as\u00ed que no se ha subido ninguna rama.",
        "spec_link": "Spec",
        "next_pr": "Revisa la pull request.",
        "next_human": "Ahora le toca a una persona.",
        "st_done": "hecha",
        "st_failed": "fallida",
        "st_skipped": "omitida",
        "escalated": "escalada a {model}",
        "st_not_started": "sin empezar",
        "hint_default_branch": "Lanza la construcci\u00f3n desde la rama por defecto.",
        "hint_branch_exists": (
            "Borra la rama (o fusiona su pull request) y vuelve a poner la etiqueta "
            "`{label}`. Specster nunca sobrescribe una rama."
        ),
        "hint_root": (
            "Lanza la fase de construcci\u00f3n con la acci\u00f3n de Docker (arranca "
            "como root); mira docs/build.md."
        ),
        "hint_pull_403": (
            "La rama se ha subido. Con GITHUB_TOKEN, activa 'Allow GitHub Actions to "
            "create and approve pull requests' en los ajustes del repositorio, o pasa un "
            "token de GitHub App."
        ),
        "hint_pull_other": (
            "La rama se ha subido, pero GitHub ha rechazado la pull request (HTTP "
            "{status}). \u00c1brela a mano desde la rama, o arregla la causa, borra la "
            "rama y vuelve a poner la etiqueta `{label}`."
        ),
        "hint_sandbox": (
            "No se ha subido nada. Mira el log del workflow, arregla la causa y vuelve a "
            "poner la etiqueta `{label}`."
        ),
        "hint_push": (
            "Comprueba que el token tiene contents: write y que la rama no existe en el "
            "remoto, y vuelve a poner la etiqueta `{label}`."
        ),
        "hint_role_model": "Revisa las credenciales del proveedor y el id del modelo en {key}.",
        "hint_checkout": "A\u00f1ade actions/checkout antes de Specster.",
        "hint_docker_socket": (
            "No se ha ejecutado nada. Quita el montaje del socket de Docker o restringe su "
            "modo y vuelve a poner la etiqueta `{label}`."
        ),
        "hint_head": (
            "Haz checkout de la rama por defecto en el commit que vio el evento "
            "(actions/checkout sin ref) y vuelve a poner la etiqueta `{label}`."
        ),
    },
}
