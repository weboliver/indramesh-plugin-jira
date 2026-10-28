import { useEffect as e, useMemo as t, useRef as n, useState as r } from "react";
import { useMutation as i, useQuery as a, useQueryClient as o } from "@tanstack/react-query";
import { Button as s, CheckboxField as c, EmptyState as l, FormField as u, LucideIcon as d, PageHeader as f, SelectInput as p, Table as m, TableSkeleton as h, api as g, confirmDialog as _, extractErrorMessage as v, interpolateDefault as y, pluginText as b, serializeRepeatedParams as x, showToast as S, useCan as C, useFormatDate as w, useObjectTypeAttributes as T, usePluginTranslation as E, useSchema as D, useSchemas as O, useServiceAccounts as k, useSources as A, useTargets as j } from "@indramesh/plugin-api";
import { Fragment as M, jsx as N, jsxs as P } from "react/jsx-runtime";
import { Link as F, useSearchParams as I } from "react-router-dom";
// ../plugins/jira/frontend/task-form-logic.js
function L({ sourceId: e = "", serviceAccountId: t = "", jiraSchemaId: n = "", targetSchemaId: r = "", includeCommentsFiles: i = !1, connectionReady: a = !1 } = {}) {
	return {
		scope: {
			source_id: e,
			service_account_id: t
		},
		plugin_parameters: {
			...n ? { jira_schema_id: n } : {},
			...n && r ? { target_schema_id: r } : {},
			include_comments_files: !!i
		},
		valid: !!(e && t && a)
	};
}
function R({ schemaId: e = "", targetId: t = "", serviceAccountId: n = "" } = {}) {
	return {
		scope: {
			schema_id: e,
			target_id: t,
			service_account_id: n
		},
		plugin_parameters: {},
		valid: !!(e && t && n)
	};
}
function z({ schemaId: e = "", targetId: t = "", serviceAccountId: n = "", jiraSchemaId: r = "", destinationEmpty: i = !1 } = {}) {
	return {
		scope: {
			schema_id: e,
			target_id: t,
			service_account_id: n
		},
		plugin_parameters: r ? { jira_schema_id: r } : {},
		valid: !!(e && t && n && r) && i === !0
	};
}
function B(e) {
	return (e || []).filter((e) => e && e.type === "jira");
}
function V(e, t = "") {
	return (e || []).filter((e) => !(!e || e.connector_type !== "jira" || e.is_active === !1 || e.schema_id && t && String(e.schema_id) !== String(t)));
}
function H(e, t = Date.now()) {
	return (e || []).filter((e) => {
		if (!e || e.is_active === !1) return !1;
		if (e.expires_at) {
			let n = Date.parse(e.expires_at);
			if (!Number.isNaN(n) && n <= t) return !1;
		}
		return !0;
	});
}
function U({ targetId: e = "", jiraSchemaId: t = "", serviceAccountId: n = "" } = {}) {
	return !e || !t || !n ? "" : `${e}:${t}:${n}`;
}
function ee({ currentKey: e = "", requestKey: t = "", loading: n = !1, data: r = null, error: i = null } = {}) {
	if (!e) return {
		status: "idle",
		count: null,
		empty: !1
	};
	if (i && t === e) return {
		status: "failed",
		count: null,
		empty: !1
	};
	if (n || !r || t !== e) return {
		status: "loading",
		count: null,
		empty: !1
	};
	let a = typeof r.object_type_count == "number" ? r.object_type_count : null;
	return r.empty === !0 ? {
		status: "empty",
		count: a,
		empty: !0
	} : {
		status: "not_empty",
		count: a,
		empty: !1
	};
}
// ../plugins/jira/frontend/connection-logic.js
var W = ["prefer_local", "prefer_remote"], G = [
	"ok",
	"auth",
	"network",
	"permission",
	"not_found",
	"error"
];
function K(e) {
	return W.includes(e) ? e : null;
}
function te(e) {
	return W.includes(e) ? e : "";
}
function q({ loaded: e = !1, fetchFailed: t = !1, connection: n = null, strategy: r = null, serviceAccountId: i = void 0, urlChanged: a = !1 } = {}) {
	if (!e || t) return { action: "skip" };
	let o = { default_conflict_strategy: r }, s = i === void 0 ? void 0 : i || null;
	if (s !== void 0 && (o.default_service_account_id = s), n === null) return {
		action: "write",
		body: o
	};
	if (n.binding === "stale" || a) return { action: "skip" };
	let c = (n.default_conflict_strategy ?? null) !== r, l = s !== void 0 && (n.default_service_account_id ?? null) !== s;
	return !c && !l ? { action: "skip" } : {
		action: "write",
		body: o
	};
}
function ne(e, t = []) {
	return e && t.some((t) => t.id === e) ? e : t.length === 1 ? t[0].id : "";
}
function re({ draftUrl: e = "", savedUrl: t = "" } = {}) {
	return String(e ?? "").trim() !== String(t ?? "").trim();
}
function ie(e) {
	return G.includes(e) ? `connector.result_${e}` : "connector.result_error";
}
function ae({ recordId: e = null, serviceAccountId: t = "", testing: n = !1 } = {}) {
	return !!e && !!t && !n;
}
// ../plugins/jira/frontend/TaskForms.jsx
function J(e, n) {
	let { data: r, isLoading: i } = k(e || null, 0, 200, n || null, { enabled: !!e || !!n });
	return {
		accounts: t(() => H(r || [], Date.now()), [r]),
		isLoading: i
	};
}
function Y(t, n, i, a, o) {
	let [s, c] = r(null);
	e(() => {
		if (!t || s === t || a) return;
		let e = ne(n?.connection?.default_service_account_id, i);
		e && (o(e), c(t));
	}, [
		t,
		n,
		i,
		a,
		s,
		o
	]);
}
function X(e) {
	return Array.isArray(e) ? e : e?.items || [];
}
function oe({ t: e }) {
	return /* @__PURE__ */ P("p", {
		className: "text-xs text-muted-foreground",
		"data-testid": "plugin-jira-no-sources",
		children: [
			e("task.no_sources", "There are no Jira sources yet. Add one under Sources."),
			" ",
			/* @__PURE__ */ N("a", {
				href: "/configuration/sources",
				className: "text-primary hover:underline underline-offset-4",
				children: e("task.action_open_sources", "Open Sources")
			})
		]
	});
}
function se({ t: e }) {
	return /* @__PURE__ */ P("p", {
		className: "text-xs text-muted-foreground",
		"data-testid": "plugin-jira-no-targets",
		children: [
			e("task.no_targets", "There are no Jira targets yet. Add one under Targets."),
			" ",
			/* @__PURE__ */ N("a", {
				href: "/configuration/targets",
				className: "text-primary hover:underline underline-offset-4",
				children: e("task.action_open_targets", "Open Targets")
			})
		]
	});
}
function Z({ t: e, kind: t }) {
	return /* @__PURE__ */ P("p", {
		className: "text-xs text-muted-foreground",
		"data-testid": "plugin-jira-no-service-accounts",
		children: [
			y(e("task.no_service_accounts", "This {{kind}} has no eligible service account yet. Add one under Service accounts."), { kind: t }),
			" ",
			/* @__PURE__ */ N("a", {
				href: "/configuration/service-accounts",
				className: "text-primary hover:underline underline-offset-4",
				children: e("task.action_open_service_accounts", "Open service accounts")
			})
		]
	});
}
function ce({ onChange: n, disabled: i }) {
	let { t: o } = E("jira"), [s, l] = r(""), [d, f] = r(""), [m, h] = r(""), [_, v] = r(""), [b, x] = r(!1);
	e(() => {
		n(L({}));
	}, []);
	let { data: S = [] } = A(0, 200), C = t(() => B(S), [S]), { accounts: w, isLoading: T } = J(s, null), { data: D, isError: O, error: k } = a({
		queryKey: [
			"plugin-jira",
			"source-connection-envelope",
			s
		],
		queryFn: () => g.get(`plugins/jira/connections/source/${s}`).then((e) => e.data),
		enabled: !!s,
		retry: !1
	}), j = !!D?.connection && !O;
	Y(s, D, w, d, f), e(() => {
		n(L({
			sourceId: s,
			serviceAccountId: d,
			jiraSchemaId: m,
			targetSchemaId: _,
			includeCommentsFiles: b,
			connectionReady: j
		}));
	}, [
		s,
		d,
		m,
		_,
		b,
		j
	]);
	let { data: M, isLoading: F, isError: I, error: R } = a({
		queryKey: [
			"plugin-jira",
			"source-schemas",
			s,
			d
		],
		queryFn: () => g.get(`plugins/jira/schemas/source/${s}`, { params: { service_account_id: d } }).then((e) => e.data),
		enabled: !!s && !!d && j
	}), z = (M?.schemas || []).map((e) => ({
		value: e.jira_schema_id,
		label: e.key ? `${e.name} (${e.key})` : e.name
	})), V = (M?.schemas || []).find((e) => String(e.jira_schema_id) === m), H = (M?.schemas || []).filter((e) => e.core_schema_id).map((e) => e.core_schema_name), U = !!V && !V.core_schema_id, { data: ee, isSuccess: W } = a({
		queryKey: [
			"plugin-jira",
			"adoptable-schemas",
			s
		],
		queryFn: () => g.get(`plugins/jira/schemas/source/${s}/adoptable`).then((e) => e.data),
		enabled: !!s && U
	}), G = (ee?.schemas || []).map((e) => ({
		value: e.id,
		label: e.keygen ? `${e.name} (${e.keygen})` : e.name
	}));
	return /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": "plugin-jira-task-form-pull",
		children: [
			/* @__PURE__ */ P("div", {
				className: "grid gap-4 sm:grid-cols-2",
				children: [C.length === 0 ? /* @__PURE__ */ N(oe, { t: o }) : /* @__PURE__ */ N(u, {
					id: "plugin-jira-pull-source",
					label: o("task.source", "Source"),
					required: !0,
					children: /* @__PURE__ */ N(p, {
						id: "plugin-jira-pull-source",
						"data-testid": "plugin-jira-pull-source",
						value: s,
						placeholder: o("task.source_placeholder", "Select a Jira source…"),
						options: C.map((e) => ({
							value: e.id,
							label: e.name
						})),
						disabled: i,
						onChange: (e) => {
							l(e.target.value), f(""), h(""), v("");
						}
					})
				}), s && w.length === 0 && !T ? /* @__PURE__ */ N(Z, {
					t: o,
					kind: "source"
				}) : /* @__PURE__ */ N(u, {
					id: "plugin-jira-pull-account",
					label: o("task.service_account", "Service account"),
					required: !0,
					children: /* @__PURE__ */ N(p, {
						id: "plugin-jira-pull-account",
						"data-testid": "plugin-jira-pull-account",
						value: d,
						placeholder: o("task.service_account_placeholder", "Select a service account…"),
						options: w.map((e) => ({
							value: e.id,
							label: e.name
						})),
						disabled: i || !s || !j,
						onChange: (e) => {
							f(e.target.value), h("");
						}
					})
				})]
			}),
			s && O && /* @__PURE__ */ N("p", {
				role: "alert",
				className: "text-xs text-destructive",
				"data-testid": "plugin-jira-pull-connection-error",
				children: k?.response?.status === 404 ? o("task.connection_missing", "This Source has no Jira plugin connection. Configure it under Sources before starting a pull.") : o("task.connection_check_failed", "The Jira plugin connection could not be verified. Pull stays blocked until the connection can be checked.")
			}),
			/* @__PURE__ */ P("div", {
				className: "grid gap-4 sm:grid-cols-2",
				children: [/* @__PURE__ */ N(u, {
					id: "plugin-jira-pull-schema",
					label: o("task.jira_schema", "Jira schema"),
					help: o("task.jira_schema_help", "Pick one schema to pull only that schema."),
					children: /* @__PURE__ */ N(p, {
						id: "plugin-jira-pull-schema",
						"data-testid": "plugin-jira-pull-schema",
						value: m,
						placeholder: o("task.jira_schema_all", "All linked Jira schemas in this Source"),
						options: z,
						disabled: i || !d || !j,
						onChange: (e) => {
							h(e.target.value), v("");
						}
					})
				}), U && /* @__PURE__ */ N(u, {
					id: "plugin-jira-pull-target-schema",
					label: o("task.target_schema", "Core schema"),
					help: o("task.target_schema_help", "Pull into an existing empty schema of this Source to keep its name and key prefix, or create a new schema named after the Jira schema."),
					children: /* @__PURE__ */ N(p, {
						id: "plugin-jira-pull-target-schema",
						"data-testid": "plugin-jira-pull-target-schema",
						value: _,
						placeholder: o("task.target_schema_new", "Create a new schema"),
						options: G,
						disabled: i,
						onChange: (e) => v(e.target.value)
					})
				})]
			}),
			U && W && G.length === 0 && /* @__PURE__ */ N("p", {
				className: "text-xs text-muted-foreground",
				"data-testid": "plugin-jira-pull-no-adoptable",
				children: o("task.target_schema_none", "This Source has no empty schema to pull into. To choose the name and key prefix yourself, create an empty schema for this Source first.")
			}),
			M && !F && !I && /* @__PURE__ */ N("p", {
				role: "status",
				className: "text-sm font-medium text-foreground",
				"data-testid": "plugin-jira-pull-core-target",
				children: V ? V.core_schema_id ? y(o("task.core_target_linked", "Core target — linked: {{schema}}"), { schema: V.core_schema_name }) : _ ? y(o("task.core_target_adopt", "Core target — the existing schema {{schema}} will be linked."), { schema: G.find((e) => e.value === _)?.label || "" }) : o("task.core_target_new", "Core target — a new schema will be created.") : H.length > 0 ? y(o("task.core_targets_all", "Core targets — linked: {{schemas}}"), { schemas: H.join(", ") }) : o("task.core_target_none", "No linked Core schema. Select a Jira schema to create a new Core schema.")
			}),
			d && F && /* @__PURE__ */ N("p", {
				className: "text-xs text-muted-foreground",
				"data-testid": "plugin-jira-schema-loading",
				children: o("task.jira_schema_loading", "Loading Jira schemas…")
			}),
			d && I && /* @__PURE__ */ N("p", {
				className: "text-xs text-destructive",
				"data-testid": "plugin-jira-schema-error",
				children: y(o("task.jira_schema_load_failed", "Couldn't load the Jira schemas: {{message}}. The pull can still run for all schemas."), { message: R?.message || "" })
			}),
			/* @__PURE__ */ N(c, {
				id: "plugin-jira-pull-comments",
				"data-testid": "plugin-jira-pull-comments",
				label: o("task.include_comments", "Include comments and attachments"),
				help: o("task.include_comments_help", "Also imports Jira comments and attachments. The pull takes longer because each object needs extra Jira requests."),
				checked: b,
				disabled: i,
				onChange: (e) => x(e.target.checked)
			})
		]
	});
}
function le({ onChange: n, disabled: i }) {
	let { t: o } = E("jira"), [s, c] = r(""), [l, d] = r(""), [f, m] = r("");
	e(() => {
		n(R({}));
	}, []), e(() => {
		n(R({
			schemaId: s,
			targetId: l,
			serviceAccountId: f
		}));
	}, [
		s,
		l,
		f
	]);
	let { data: h = [] } = O(null, 0, 200, { enabled: !0 }), _ = X(h), { data: v } = j(0, 200), y = t(() => V(X(v), s), [v, s]), { accounts: b, isLoading: x } = J(null, l), { data: S } = a({
		queryKey: [
			"plugin-jira",
			"target-connection",
			l
		],
		queryFn: () => g.get(`plugins/jira/connections/target/${l}`).then((e) => e.data),
		enabled: !!l,
		retry: !1
	});
	return Y(l, S, b, f, m), /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": "plugin-jira-task-form-push",
		children: [
			/* @__PURE__ */ N(u, {
				id: "plugin-jira-push-schema",
				label: o("task.schema", "Schema"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-push-schema",
					"data-testid": "plugin-jira-push-schema",
					value: s,
					placeholder: o("task.schema_placeholder", "Select a schema…"),
					options: _.map((e) => ({
						value: e.id,
						label: e.name
					})),
					disabled: i,
					onChange: (e) => {
						c(e.target.value), d(""), m("");
					}
				})
			}),
			y.length === 0 ? /* @__PURE__ */ N(se, { t: o }) : /* @__PURE__ */ N(u, {
				id: "plugin-jira-push-target",
				label: o("task.target", "Target"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-push-target",
					"data-testid": "plugin-jira-push-target",
					value: l,
					placeholder: o("task.target_placeholder", "Select a Target…"),
					options: y.map((e) => ({
						value: e.id,
						label: e.name
					})),
					disabled: i,
					onChange: (e) => {
						d(e.target.value), m("");
					}
				})
			}),
			l && b.length === 0 && !x ? /* @__PURE__ */ N(Z, {
				t: o,
				kind: "target"
			}) : /* @__PURE__ */ N(u, {
				id: "plugin-jira-push-account",
				label: o("task.service_account", "Service account"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-push-account",
					"data-testid": "plugin-jira-push-account",
					value: f,
					placeholder: o("task.service_account_placeholder", "Select a service account…"),
					options: b.map((e) => ({
						value: e.id,
						label: e.name
					})),
					disabled: i || !l,
					onChange: (e) => m(e.target.value)
				})
			})
		]
	});
}
function ue(e, t) {
	switch (t.status) {
		case "loading": return e("task.precheck_checking", "Checking whether the destination is empty…");
		case "empty": return e("task.precheck_empty", "The destination is empty. Ready to export.");
		case "not_empty": return y(e(t.count === 1 ? "task.precheck_not_empty_one" : "task.precheck_not_empty_other", t.count === 1 ? "The destination already has {{count}} object type. Export needs an empty Jira schema, so pick an empty one or push instead." : "The destination already has {{count}} object types. Export needs an empty Jira schema, so pick an empty one or push instead."), { count: t.count ?? 0 });
		case "failed": return null;
		default: return null;
	}
}
function de({ onChange: n, disabled: i }) {
	let { t: o } = E("jira"), [c, l] = r(""), [f, m] = r(""), [h, _] = r(""), [v, b] = r(""), [x, S] = r(0), { data: C = [] } = O(null, 0, 200, { enabled: !0 }), w = X(C), { data: T } = j(0, 200), D = t(() => V(X(T), c), [T, c]), { accounts: k, isLoading: A } = J(null, f), { data: M, isLoading: F, isError: I, error: L } = a({
		queryKey: [
			"plugin-jira",
			"target-schemas",
			f,
			h
		],
		queryFn: () => g.get(`plugins/jira/schemas/target/${f}`, { params: { service_account_id: h } }).then((e) => e.data),
		enabled: !!f && !!h
	}), R = M?.schemas || [], B = U({
		targetId: f,
		jiraSchemaId: v,
		serviceAccountId: h
	}), { data: H, isLoading: W, isError: G, error: K, refetch: te } = a({
		queryKey: [
			"plugin-jira",
			"destination-count",
			f,
			v,
			h,
			x
		],
		queryFn: ({ queryKey: [, , e, t, n] }) => {
			let r = U({
				targetId: e,
				jiraSchemaId: t,
				serviceAccountId: n
			});
			return g.get(`plugins/jira/schemas/target/${e}/${t}/object-type-count`, { params: { service_account_id: n } }).then((e) => ({
				requestKey: r,
				data: e.data
			}), (e) => {
				throw Object.assign(e || /* @__PURE__ */ Error("preflight failed"), { preflightRequestKey: r });
			});
		},
		enabled: !!B
	}), q = ee({
		currentKey: B,
		requestKey: (G ? K?.preflightRequestKey : H?.requestKey) || "",
		loading: W,
		data: H?.data ?? null,
		error: G ? K || /* @__PURE__ */ Error("preflight failed") : null
	});
	e(() => {
		n(z({
			schemaId: c,
			targetId: f,
			serviceAccountId: h,
			jiraSchemaId: v,
			destinationEmpty: q.empty
		}));
	}, [
		c,
		f,
		h,
		v,
		q.empty
	]);
	let ne = ue(o, q);
	return /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": "plugin-jira-task-form-export",
		children: [
			/* @__PURE__ */ N(u, {
				id: "plugin-jira-export-schema",
				label: o("task.schema", "Schema"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-export-schema",
					"data-testid": "plugin-jira-export-schema",
					value: c,
					placeholder: o("task.schema_placeholder", "Select a schema…"),
					options: w.map((e) => ({
						value: e.id,
						label: e.name
					})),
					disabled: i,
					onChange: (e) => {
						l(e.target.value), m(""), _(""), b("");
					}
				})
			}),
			D.length === 0 ? /* @__PURE__ */ N(se, { t: o }) : /* @__PURE__ */ N(u, {
				id: "plugin-jira-export-target",
				label: o("task.target", "Target"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-export-target",
					"data-testid": "plugin-jira-export-target",
					value: f,
					placeholder: o("task.target_placeholder", "Select a Target…"),
					options: D.map((e) => ({
						value: e.id,
						label: e.name
					})),
					disabled: i,
					onChange: (e) => {
						m(e.target.value), _(""), b("");
					}
				})
			}),
			f && k.length === 0 && !A ? /* @__PURE__ */ N(Z, {
				t: o,
				kind: "target"
			}) : /* @__PURE__ */ N(u, {
				id: "plugin-jira-export-account",
				label: o("task.service_account", "Service account"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-export-account",
					"data-testid": "plugin-jira-export-account",
					value: h,
					placeholder: o("task.service_account_placeholder", "Select a service account…"),
					options: k.map((e) => ({
						value: e.id,
						label: e.name
					})),
					disabled: i || !f,
					onChange: (e) => {
						_(e.target.value), b("");
					}
				})
			}),
			/* @__PURE__ */ N(u, {
				id: "plugin-jira-export-destination",
				label: o("task.destination", "Destination Jira schema"),
				required: !0,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-export-destination",
					"data-testid": "plugin-jira-export-destination",
					value: v,
					placeholder: o("task.destination_placeholder", "Select a destination…"),
					options: R.map((e) => ({
						value: e.jira_schema_id,
						label: e.key ? `${e.name} (${e.key})` : e.name
					})),
					disabled: i || !h,
					onChange: (e) => b(e.target.value)
				})
			}),
			h && F && /* @__PURE__ */ N("p", {
				className: "text-xs text-muted-foreground",
				"data-testid": "plugin-jira-destination-loading",
				children: o("task.jira_schema_loading", "Loading Jira schemas…")
			}),
			h && I && /* @__PURE__ */ N("p", {
				className: "text-xs text-destructive",
				"data-testid": "plugin-jira-destination-error",
				children: y(o("task.destination_load_failed", "Couldn't load the Jira schemas for this Target: {{message}}. Check the service account, then choose the Target again."), { message: L?.message || "" })
			}),
			q.status !== "idle" && /* @__PURE__ */ P("div", {
				className: "flex items-center gap-2 text-xs",
				"data-testid": "plugin-jira-export-precheck",
				children: [
					q.status === "empty" ? /* @__PURE__ */ N(d, {
						name: "circle-check",
						size: 12,
						className: "text-green-700 dark:text-green-400",
						"aria-hidden": "true"
					}) : q.status === "loading" ? /* @__PURE__ */ N(d, {
						name: "loader-circle",
						size: 12,
						className: "text-muted-foreground motion-reduce:animate-none",
						"aria-hidden": "true"
					}) : /* @__PURE__ */ N(d, {
						name: "circle-x",
						size: 12,
						className: "text-destructive",
						"aria-hidden": "true"
					}),
					/* @__PURE__ */ N("span", {
						role: "status",
						"aria-live": "polite",
						className: q.status === "empty" ? "text-green-700 dark:text-green-400" : q.status === "loading" ? "text-muted-foreground" : "text-destructive",
						children: q.status === "failed" ? y(o("task.precheck_failed", "Couldn't check the destination: {{message}}. Export stays blocked until the check succeeds."), { message: K?.message || "" }) : ne
					}),
					q.status === "failed" && /* @__PURE__ */ N(s, {
						type: "button",
						variant: "ghost",
						size: "sm",
						"data-testid": "plugin-jira-export-recheck",
						onClick: () => {
							S((e) => e + 1), te();
						},
						children: o("task.precheck_retry", "Check again")
					})
				]
			})
		]
	});
}
// ../plugins/jira/frontend/JiraConnectionEditor.jsx
var fe = {
	source: "source",
	target: "target"
}, pe = {
	source: "source_url",
	target: "target_url"
}, me = {
	ok: "Connected. Jira accepted the service account.",
	auth: "Jira rejected the service account's credentials. Update its token under Service accounts, then test again.",
	permission: "Jira accepted the sign-in but the account can't read Assets schemas. Grant it Assets access in Jira, then test again.",
	network: "Jira didn't respond at {{url}}. Check the address and that Jira is reachable from IndraMesh.",
	not_found: "Jira has no schema with this ID. Check the schema, then test again.",
	error: "The connection test failed. Check the address and service account, then test again."
};
function he(e, t, n) {
	let r = b("jira", e, { defaultValue: t });
	return n ? y(r, n) : r;
}
async function ge({ role: e, record: t, previousRecord: n, draft: r } = {}) {
	let i = fe[e], a = t?.id;
	if (!i || !a) return;
	let o = pe[i], s = n ? re({
		draftUrl: t?.[o],
		savedUrl: n?.[o]
	}) : !1, c = q({
		loaded: r?.loaded === !0,
		fetchFailed: r?.fetchFailed === !0,
		connection: r?.connection ?? null,
		strategy: r?.strategy ?? null,
		serviceAccountId: r?.serviceAccountId,
		urlChanged: s
	});
	if (c.action === "write") try {
		await g.put(`plugins/jira/connections/${i}/${a}`, c.body);
	} catch (e) {
		let t = e?.response?.status;
		throw Error(t === 422 ? he("connector.bind_invalid_url", "The {{kind}} needs a valid http(s) address before the Jira connection can be saved.", { kind: i }) : t === 404 ? he("connector.bind_not_jira", "This {{kind}} is no longer a Jira {{kind}}, so no Jira connection can be saved.", { kind: i }) : he("connector.bind_failed", "The Jira connection could not be saved. Check the address and try again."));
	}
}
function _e({ role: i, record: o, draft: c = {}, onDraftChange: l, draftFields: f = {} }) {
	let { t: m } = E("jira"), h = fe[i] || "source", _ = o?.id || null, v = n(c);
	v.current = c;
	let { data: b, isLoading: x, isError: S } = a({
		queryKey: [
			"plugin-jira",
			"connection",
			h,
			_
		],
		queryFn: async () => {
			try {
				return (await g.get(`plugins/jira/connections/${h}/${_}`)).data?.connection ?? null;
			} catch (e) {
				if (e?.response?.status === 404) return null;
				throw e;
			}
		},
		enabled: !!_,
		retry: !1
	});
	e(() => {
		let e = v.current || {};
		_ ? x || l(S ? {
			...e,
			connection: null,
			loaded: !1,
			fetchFailed: !0
		} : {
			...e,
			strategy: b ? b.default_conflict_strategy ?? null : e.strategy ?? null,
			connection: b ?? null,
			loaded: !0,
			fetchFailed: !1
		}) : l({
			...e,
			connection: null,
			loaded: !0,
			fetchFailed: !1
		});
	}, [
		_,
		x,
		S,
		b
	]);
	let C = c.connection ?? null, w = c.loaded === !0, T = c.fetchFailed === !0, D = C?.binding === "stale", O = te(c.strategy ?? null), A = o?.[pe[h]] ?? "", j = !!_ && re({
		draftUrl: f.url,
		savedUrl: A
	}), M = !w || T || D, F = h === "source" ? m("connector.role_help_source", "Used when pulling from Jira.") : m("connector.role_help_target", "Used when pushing to Jira."), [I, L] = r(""), [R, z] = r(!1), [B, V] = r(null), { data: U, isLoading: ee } = k(h === "source" ? _ : null, 0, 50, h === "target" ? _ : null, { enabled: !!_ }), W = t(() => H(U || [], Date.now()), [U]), [G, q] = r(!1);
	e(() => {
		if (G) return;
		let e = ne(b?.default_service_account_id, W);
		e && L(e);
	}, [
		b,
		W,
		G
	]), e(() => {
		V(null);
	}, [I, _]);
	let J = !!_, Y = J && !ee && W.length === 0, X = !ae({
		recordId: _,
		serviceAccountId: I,
		testing: R
	}), oe = async () => {
		if (ae({
			recordId: _,
			serviceAccountId: I,
			testing: R
		})) {
			z(!0), V(null);
			try {
				let e = await g.post(`plugins/jira/connections/${h}/${_}/test`, { service_account_id: I });
				V({ category: e.data?.category ?? "error" });
			} catch {
				V({ category: "error" });
			} finally {
				z(!1);
			}
		}
	}, se = /* @__PURE__ */ N("a", {
		href: "/configuration/service-accounts",
		className: "text-primary hover:underline underline-offset-4",
		children: m("connector.manage_link", "Manage service accounts")
	}), Z = B?.category ?? "error", ce = Z === "ok", le = m(ie(Z), me[Z] || me.error, { url: A || "—" });
	return /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": "plugin-jira-connection-editor",
		children: [
			/* @__PURE__ */ N(u, {
				id: "plugin-jira-connector-strategy",
				label: m("connector.strategy_label", "Default conflict handling"),
				help: F,
				children: /* @__PURE__ */ N(p, {
					id: "plugin-jira-connector-strategy",
					"data-testid": "plugin-jira-strategy",
					value: O,
					disabled: M,
					options: [
						{
							value: "",
							label: m("connector.strategy_manual", "Review manually")
						},
						{
							value: "prefer_local",
							label: m("connector.strategy_prefer_local", "Prefer IndraMesh values")
						},
						{
							value: "prefer_remote",
							label: m("connector.strategy_prefer_remote", "Prefer Jira values")
						}
					],
					onChange: (e) => {
						l({
							...v.current || {},
							strategy: K(e.target.value)
						});
					}
				})
			}),
			/* @__PURE__ */ N("p", {
				className: "text-xs text-muted-foreground",
				"data-testid": "plugin-jira-strategy-scope",
				children: m("connector.scope_help", "Applies to conflicts found from now on. Conflicts that are already open keep their state.")
			}),
			T && /* @__PURE__ */ N("p", {
				className: "text-xs text-destructive",
				"data-testid": "plugin-jira-connection-load-error",
				children: m("connector.load_failed", "Couldn't load the Jira connection settings. Reopen the dialog to try again.")
			}),
			D && /* @__PURE__ */ N("p", {
				className: "text-xs text-amber-600 dark:text-amber-500",
				"data-testid": "plugin-jira-stale",
				children: m("connector.stale_state", "The address changed after this connection was bound, so Jira synchronisation is locked for this record. Re-binding is not available yet.")
			}),
			j && !D && w && !T && /* @__PURE__ */ N("p", {
				className: "text-xs text-amber-600 dark:text-amber-500",
				"data-testid": "plugin-jira-url-changed",
				children: m("connector.url_changed_warning", "You changed the address. Saving it locks Jira synchronisation for this record because re-binding is not available yet.")
			}),
			/* @__PURE__ */ P("div", {
				className: "space-y-2",
				"data-testid": "plugin-jira-connection-test",
				children: [
					J && /* @__PURE__ */ N(u, {
						id: "plugin-jira-test-account",
						label: m("task.service_account", "Service account"),
						help: m("connector.account_help", "Saved with the connector settings and preselected for the connection test and Jira tasks."),
						children: /* @__PURE__ */ N(p, {
							id: "plugin-jira-test-account",
							"data-testid": "plugin-jira-test-account",
							value: I,
							placeholder: m("task.service_account_placeholder", "Select a service account…"),
							options: W.map((e) => ({
								value: e.id,
								label: e.name
							})),
							disabled: Y || R,
							onChange: (e) => {
								L(e.target.value), q(!0), l({
									...v.current || {},
									serviceAccountId: e.target.value
								});
							}
						})
					}),
					J && /* @__PURE__ */ N(s, {
						type: "button",
						variant: "outline",
						size: "sm",
						leftIcon: "plug-zap",
						loading: R,
						disabled: X,
						"data-testid": "plugin-jira-test-button",
						onClick: oe,
						children: m("connector.test_button", "Test connection")
					}),
					J && /* @__PURE__ */ N("p", {
						className: "text-xs text-muted-foreground",
						"data-testid": "plugin-jira-tested-url",
						children: j ? m("connector.unsaved_url_hint", "Save first to test the address you just entered.") : y(m("connector.tested_url_hint", "Tests the saved address {{url}}."), { url: A || "—" })
					}),
					!J && /* @__PURE__ */ P("p", {
						className: "text-xs text-muted-foreground",
						"data-testid": "plugin-jira-test-new-record",
						children: [
							y(m("connector.new_record_test_disabled", "Save this {{kind}} first. The connection test is available afterwards: assign a service account to this {{kind}} and test here."), { kind: h }),
							" ",
							se
						]
					}),
					Y && /* @__PURE__ */ P("p", {
						className: "text-xs text-muted-foreground",
						"data-testid": "plugin-jira-test-no-accounts",
						children: [
							y(m("connector.no_account_test_disabled", "Add a service account for this {{kind}} to test the connection."), { kind: h }),
							" ",
							se
						]
					}),
					B && /* @__PURE__ */ P("p", {
						role: "status",
						"aria-live": "polite",
						"data-testid": "plugin-jira-test-result",
						className: ce ? "flex items-center gap-2 text-sm text-green-700 dark:text-green-400" : "flex items-center gap-2 text-sm text-destructive",
						children: [/* @__PURE__ */ N(d, {
							name: ce ? "circle-check" : "circle-x",
							size: 12,
							"aria-hidden": "true"
						}), le]
					})
				]
			})
		]
	});
}
// ../plugins/jira/frontend/direction-logic.js
var ve = [
	"none",
	"pull",
	"push",
	"both"
];
function ye(e) {
	return ve.includes(e) ? e : null;
}
function be(e) {
	return ve.includes(e) ? e : "";
}
function xe(e, { schemaLevel: t = !1 } = {}) {
	return e == null ? t ? "direction.default_both" : "direction.inherit" : `direction.${e}`;
}
function Se(e) {
	return e === "attribute" || e === "object_type" || e === "schema" ? `direction.origin_${e}` : "direction.origin_default";
}
function Ce({ value: e }) {
	return {
		status: "saving",
		value: e,
		error: null
	};
}
function we({ explicit_direction: e, effective_direction: t, direction_origin: n } = {}) {
	return {
		status: "saved",
		value: e ?? null,
		effective: t,
		origin: n,
		error: null
	};
}
function Te({ previousValue: e, message: t } = {}) {
	return {
		status: "error",
		value: e ?? null,
		error: t
	};
}
function Ee(e, t = 100) {
	if (!Number.isInteger(t) || t < 1) throw RangeError("chunk size must be an integer >= 1");
	let n = [];
	for (let r = 0; r < e.length; r += t) n.push(e.slice(r, r + t));
	return n;
}
var De = /^[0-9]{1,20}$/;
function Q(e) {
	let t = String(e ?? "");
	return De.test(t) ? t : null;
}
function Oe(e) {
	let t = Q(e);
	return t === null ? null : `/secure/ObjectSchema.jspa?id=${t}`;
}
function ke(e, t) {
	let n = Q(e), r = Q(t);
	return n === null || r === null ? null : `/secure/ObjectSchema.jspa?id=${n}&typeId=${r}`;
}
function Ae(e) {
	let t = Q(e);
	return t === null ? null : `/secure/ShowObject.jspa?id=${t}`;
}
function je(e, t) {
	if (typeof e != "string" || typeof t != "string" || !t.startsWith("/")) return null;
	let n;
	try {
		n = new URL(e);
	} catch {
		return null;
	}
	if (n.protocol !== "http:" && n.protocol !== "https:" || n.username || n.password) return null;
	let r = n.pathname.endsWith("/") ? n.pathname : `${n.pathname}/`, i;
	try {
		i = new URL(t.replace(/^\/+/, ""), `${n.origin}${r}`);
	} catch {
		return null;
	}
	return i.protocol !== "http:" && i.protocol !== "https:" || i.username || i.password || i.origin !== n.origin ? null : i.toString();
}
// ../plugins/jira/frontend/JiraSections.jsx
var Me = {
	SUCCESS: "completed",
	PARTIAL_SUCCESS: "completed",
	FAILED: "failed",
	CANCELLED: "cancelled",
	RUNNING: "running",
	PENDING: "queued"
};
function Ne(e, t) {
	let n = (t || []).join(",");
	return a({
		queryKey: [
			"plugin-jira",
			"links",
			e,
			n
		],
		queryFn: async () => {
			let n = x({ ids: t || [] });
			return (await g.get(`plugins/jira/links/${e}?${n}`)).data?.items || [];
		},
		enabled: (t || []).length > 0,
		retry: !1
	});
}
function Pe(e) {
	return a({
		queryKey: [
			"plugin-jira",
			"schema-status",
			e
		],
		queryFn: async () => (await g.get(`plugins/jira/schemas/${e}/sync-status`)).data,
		enabled: !!e,
		retry: !1
	});
}
function Fe(e, t) {
	return a({
		queryKey: [
			"plugin-jira",
			"source-connection",
			e
		],
		queryFn: async () => (await g.get(`plugins/jira/connections/source/${e}`)).data?.connection || null,
		enabled: !!e && !!t,
		retry: !1
	});
}
function Ie(e, t, n) {
	let r = Fe(t, n && !!e).data;
	return !e || !r || r.endpoint_kind !== "source" || String(r.endpoint_id) !== String(t) || r.binding !== "live" ? null : je(r.base_url, e);
}
function Le({ jiraId: e, href: t }) {
	let { t: n } = E("jira");
	return t ? /* @__PURE__ */ P("a", {
		href: t,
		target: "_blank",
		rel: "noopener noreferrer",
		className: "inline-flex items-center gap-1 text-primary hover:underline underline-offset-4 tabular-nums",
		"data-testid": "plugin-jira-id-link",
		"aria-label": y(n("link.open_in_jira", "Open {{id}} in Jira (opens in a new tab)"), { id: e }),
		children: [e, /* @__PURE__ */ N(d, {
			name: "external-link",
			size: 12,
			"aria-hidden": "true"
		})]
	}) : /* @__PURE__ */ N("span", {
		className: "tabular-nums",
		children: e
	});
}
function Re() {
	let { t: e } = E("jira");
	return /* @__PURE__ */ P("p", {
		className: "flex items-center gap-2 text-sm text-muted-foreground",
		"data-testid": "plugin-jira-section-loading",
		children: [/* @__PURE__ */ N(d, {
			name: "loader-circle",
			size: 14,
			className: "animate-spin motion-reduce:animate-none",
			"aria-hidden": "true"
		}), e("section.loading", "Loading Jira details…")]
	});
}
function ze({ error: e, onRetry: t }) {
	let { t: n } = E("jira");
	return /* @__PURE__ */ P("div", {
		className: "space-y-2",
		"data-testid": "plugin-jira-section-error",
		children: [/* @__PURE__ */ N("p", {
			role: "alert",
			className: "text-sm text-destructive",
			children: y(n("section.load_error", "Couldn't load the Jira details: {{message}}."), { message: v(e) })
		}), /* @__PURE__ */ N(s, {
			type: "button",
			variant: "outline",
			size: "sm",
			onClick: t,
			children: n("section.retry", "Try again")
		})]
	});
}
function Be({ children: e }) {
	return /* @__PURE__ */ N("dl", {
		className: "grid gap-4 sm:grid-cols-2",
		children: e
	});
}
function $({ label: e, children: t }) {
	return /* @__PURE__ */ P("div", { children: [/* @__PURE__ */ N("dt", {
		className: "text-xs text-muted-foreground",
		children: e
	}), /* @__PURE__ */ N("dd", {
		className: "mt-1 text-sm",
		children: t
	})] });
}
function Ve({ run: e, neverKey: t, neverFallback: n }) {
	let { t: r } = E("jira"), i = w();
	if (!e) return /* @__PURE__ */ N("span", {
		className: "text-muted-foreground",
		children: r(t, n)
	});
	let a = Me[e.status] || "queued", o = e.completed_at || e.started_at, s = a === "failed" ? "circle-x" : a === "running" ? "loader-circle" : "circle-check", c = a === "queued" ? r("section.run_queued", "Queued") : r(`section.run_${a}`, "Completed {{date}}", { date: i(o) });
	return /* @__PURE__ */ P("span", {
		className: "block space-y-1",
		children: [/* @__PURE__ */ P("span", {
			className: "flex items-center gap-2",
			children: [/* @__PURE__ */ N(d, {
				name: s,
				size: 12,
				className: a === "running" ? "animate-spin motion-reduce:animate-none" : void 0,
				"aria-hidden": "true"
			}), c]
		}), e.status === "FAILED" && e.error_message && /* @__PURE__ */ N("span", {
			className: "block whitespace-pre-wrap break-words text-xs text-destructive",
			children: e.error_message
		})]
	});
}
function He(e) {
	return {
		status: "idle",
		value: e?.explicit_direction ?? null,
		effective: e?.effective_direction ?? null,
		origin: e?.direction_origin ?? null,
		error: null
	};
}
function Ue({ entity: t, entityId: n, item: i, schemaLevel: a = !1 }) {
	let { t: s } = E("jira"), c = C("sync", "trigger"), l = o(), [u, f] = r(() => He(i));
	if (e(() => {
		f(He(i));
	}, [
		i?.explicit_direction,
		i?.effective_direction,
		i?.direction_origin
	]), !i?.linked) return null;
	let m = u.effective, h = u.origin, _ = s(`direction.${m}`, m || ""), b = async (e) => {
		let r = u.value;
		f(Ce({ value: e }));
		try {
			let r = await g.put(`plugins/jira/directions/${t}/${n}`, { direction: e });
			f(we(r.data || {})), l.invalidateQueries({ queryKey: ["plugin-jira", "links"] }), l.invalidateQueries({ queryKey: ["object-type-attributes"] }), l.invalidateQueries({ queryKey: ["objects"] });
		} catch (e) {
			f(Te({
				previousValue: r,
				message: v(e)
			}));
		}
	}, x = /* @__PURE__ */ P("span", {
		className: "block text-xs text-muted-foreground",
		children: [
			y(s("direction.effective", "Effective: {{direction}}."), { direction: _ }),
			" ",
			s(Se(h), We(h))
		]
	});
	return /* @__PURE__ */ P("div", {
		className: "space-y-1",
		children: [
			c ? /* @__PURE__ */ P("div", {
				className: "flex flex-wrap items-center gap-2",
				children: [
					/* @__PURE__ */ N(p, {
						className: "w-auto",
						"aria-label": s("direction.label", "Sync direction"),
						"data-testid": `plugin-jira-direction-${t}`,
						value: be(u.value),
						disabled: u.status === "saving",
						options: [{
							value: "",
							label: s(xe(null, { schemaLevel: a }), a ? "Default (both directions)" : "Inherit")
						}, ...ve.map((e) => ({
							value: e,
							label: s(xe(e), e)
						}))],
						onChange: (e) => b(ye(e.target.value))
					}),
					u.status === "saving" && /* @__PURE__ */ N("span", {
						className: "text-xs text-muted-foreground",
						children: s("direction.saving", "Saving…")
					}),
					u.status === "saved" && /* @__PURE__ */ P("span", {
						className: "flex items-center gap-1 text-xs text-green-700 dark:text-green-400",
						children: [/* @__PURE__ */ N(d, {
							name: "circle-check",
							size: 12,
							"aria-hidden": "true"
						}), s("direction.saved", "Saved")]
					})
				]
			}) : /* @__PURE__ */ N("span", {
				className: "block text-sm",
				"data-testid": `plugin-jira-direction-${t}-readonly`,
				children: s(xe(u.value, { schemaLevel: a }), u.value || "")
			}),
			x,
			u.status === "error" && /* @__PURE__ */ N("span", {
				role: "alert",
				className: "block text-xs text-destructive",
				children: y(s("direction.save_error", "Couldn't save the direction: {{message}}. The previous setting still applies."), { message: u.error })
			}),
			m === "pull" && /* @__PURE__ */ P("span", {
				className: "flex items-center gap-2 text-xs text-muted-foreground",
				children: [/* @__PURE__ */ N(d, {
					name: "lock",
					size: 12,
					"aria-hidden": "true"
				}), s("direction.lock_hint", "Locked in IndraMesh: values change only through a Jira pull.")]
			})
		]
	});
}
function We(e) {
	switch (e) {
		case "attribute": return "Set here.";
		case "object_type": return "Inherited from the object type.";
		case "schema": return "Inherited from the schema.";
		default: return "Default because no level sets a direction.";
	}
}
function Ge({ conflicts: e, capped: t }) {
	let { t: n } = E("jira"), r = C("conflicts", "read"), i = t?.pull ? "100+" : String(e?.pull ?? 0), a = t?.push ? "100+" : String(e?.push ?? 0), o = (e?.pull ?? 0) + (e?.push ?? 0) === 0 && !t?.pull && !t?.push;
	return /* @__PURE__ */ P("span", {
		className: "block space-y-1",
		"data-testid": "plugin-jira-open-conflicts",
		children: [o ? n("section.conflicts_none", "None") : y(n("section.conflicts_summary", "{{pull}} from pulls, {{push}} from pushes"), {
			pull: i,
			push: a
		}), !o && r && /* @__PURE__ */ N("a", {
			href: "/plugins/jira/conflicts?status=pending",
			className: "block text-primary hover:underline underline-offset-4",
			children: n("section.review_conflicts", "Review conflicts")
		})]
	});
}
function Ke({ schema: t }) {
	let { t: n } = E("jira"), i = o(), c = C("sync", "trigger"), l = t?.source_id, u = t?.id, d = Fe(l, c).data, f = a({
		queryKey: [
			"plugin-jira",
			"adoptable-schemas",
			l
		],
		queryFn: async () => (await g.get(`plugins/jira/schemas/source/${l}/adoptable`)).data?.schemas || [],
		enabled: !!l && c && !!d,
		retry: !1
	}), m = (f.data || []).some((e) => e.id === u), { data: h } = k(l, 0, 50, null, { enabled: !!l && m }), _ = H(h || [], Date.now()), [b, x] = r(""), [w, T] = r(""), [D, O] = r(!1);
	e(() => {
		if (b) return;
		let e = ne(d?.default_service_account_id, _);
		e && x(e);
	}, [
		d,
		_,
		b
	]);
	let A = a({
		queryKey: [
			"plugin-jira",
			"source-schemas",
			l,
			b
		],
		queryFn: async () => (await g.get(`plugins/jira/schemas/source/${l}`, { params: { service_account_id: b } })).data?.schemas || [],
		enabled: m && !!b,
		retry: !1
	}), j = (A.data || []).filter((e) => !e.core_schema_id).map((e) => ({
		value: String(e.jira_schema_id),
		label: e.key ? `${e.name} (${e.key})` : e.name
	}));
	return !c || !d || d.binding !== "live" || f.isLoading ? null : m ? /* @__PURE__ */ P("div", {
		className: "space-y-2",
		"data-testid": "plugin-jira-schema-link",
		children: [
			/* @__PURE__ */ N("p", {
				className: "text-xs text-muted-foreground",
				children: n("section.link_help", "Link this empty schema to a Jira schema. Nothing is imported yet: the next pull for this Source fills it and keeps this name and key prefix.")
			}),
			/* @__PURE__ */ N(p, {
				id: "plugin-jira-schema-link-account",
				"data-testid": "plugin-jira-schema-link-account",
				value: b,
				placeholder: n("task.service_account_placeholder", "Select a service account…"),
				options: _.map((e) => ({
					value: e.id,
					label: e.name
				})),
				disabled: D,
				onChange: (e) => {
					x(e.target.value), T("");
				}
			}),
			/* @__PURE__ */ N(p, {
				id: "plugin-jira-schema-link-jira",
				"data-testid": "plugin-jira-schema-link-jira",
				value: w,
				placeholder: A.isLoading ? n("task.jira_schema_loading", "Loading Jira schemas…") : n("section.link_jira_placeholder", "Select a Jira schema…"),
				options: j,
				disabled: D || !b,
				onChange: (e) => T(e.target.value)
			}),
			A.isError && /* @__PURE__ */ N("p", {
				className: "text-xs text-destructive",
				"data-testid": "plugin-jira-schema-link-load-error",
				children: y(n("section.link_load_failed", "Couldn't load the Jira schemas: {{message}}."), { message: v(A.error) })
			}),
			/* @__PURE__ */ N(s, {
				type: "button",
				variant: "outline",
				size: "sm",
				leftIcon: "link",
				loading: D,
				disabled: !b || !w || D,
				"data-testid": "plugin-jira-schema-link-button",
				onClick: async () => {
					O(!0);
					try {
						await g.post(`plugins/jira/schemas/${u}/link`, {
							jira_schema_id: w,
							service_account_id: b
						}), S(n("section.link_toast", "Linked to Jira. The next pull for this Source fills this schema.")), i.invalidateQueries({ queryKey: ["plugin-jira", "links"] }), i.invalidateQueries({ queryKey: [
							"plugin-jira",
							"schema-status",
							u
						] }), i.invalidateQueries({ queryKey: [
							"plugin-jira",
							"adoptable-schemas",
							l
						] }), i.invalidateQueries({ queryKey: [
							"plugin-jira",
							"source-schemas",
							l
						] });
					} catch (e) {
						S(y(n("section.link_error", "Couldn't link this schema: {{message}}."), { message: v(e) }), { type: "error" });
					} finally {
						O(!1);
					}
				},
				children: n("section.link_button", "Link to Jira schema")
			})
		]
	}) : /* @__PURE__ */ N("p", {
		className: "text-xs text-muted-foreground",
		"data-testid": "plugin-jira-schema-link-not-empty",
		children: n("section.link_requires_empty", "Only an empty schema (without object types) can be linked to an existing Jira schema here.")
	});
}
function qe({ schema: e }) {
	let { t } = E("jira"), n = e?.id, r = Pe(n), i = Ne("schema", n ? [n] : []), a = i.data?.[0] || null, o = !!a?.linked, s = Ie(o ? Oe(a.jira_id) : null, e?.source_id, o);
	if (!n) return null;
	if (r.isLoading || i.isLoading) return /* @__PURE__ */ N(Re, {});
	if (r.isError || i.isError) return /* @__PURE__ */ N(ze, {
		error: r.error || i.error,
		onRetry: () => {
			r.refetch(), i.refetch();
		}
	});
	let c = r.data || {}, l = c.link, u = c.open_checkpoints || [];
	return /* @__PURE__ */ P("div", {
		className: "space-y-6",
		"data-testid": "plugin-jira-schema-section",
		children: [/* @__PURE__ */ P("div", {
			className: "space-y-3",
			children: [
				/* @__PURE__ */ N("h3", {
					className: "text-sm font-semibold",
					children: t("section.heading_link_direction", "Link and direction")
				}),
				/* @__PURE__ */ P(Be, { children: [/* @__PURE__ */ N($, {
					label: t("section.schema_id_label", "Jira schema"),
					children: o ? /* @__PURE__ */ N(Le, {
						jiraId: a.jira_id,
						href: s
					}) : /* @__PURE__ */ P("div", {
						className: "space-y-2",
						children: [/* @__PURE__ */ N("span", {
							className: "text-muted-foreground",
							children: t("section.unlinked_schema", "This schema isn't linked to Jira. Run a Jira pull for its Source to link it.")
						}), /* @__PURE__ */ N(Ke, { schema: e })]
					})
				}), o && /* @__PURE__ */ N($, {
					label: t("direction.label", "Sync direction"),
					children: /* @__PURE__ */ N(Ue, {
						entity: "schema",
						entityId: n,
						item: a,
						schemaLevel: !0
					})
				})] }),
				l?.binding === "stale" && /* @__PURE__ */ P("p", {
					className: "flex items-start gap-2 text-xs text-amber-600 dark:text-amber-500",
					"data-testid": "plugin-jira-schema-stale",
					children: [/* @__PURE__ */ N(d, {
						name: "triangle-alert",
						size: 12,
						"aria-hidden": "true"
					}), t("section.stale", "Out of date: the Source now points to a different Jira address. Run a pull to relink this schema.")]
				})
			]
		}), /* @__PURE__ */ P("div", {
			className: "space-y-3",
			children: [/* @__PURE__ */ N("h3", {
				className: "text-sm font-semibold",
				children: t("section.heading_status", "Status")
			}), /* @__PURE__ */ P(Be, { children: [
				/* @__PURE__ */ N($, {
					label: t("section.last_pull", "Last pull"),
					children: /* @__PURE__ */ N(Ve, {
						run: c.last_pull,
						neverKey: "section.never_pulled",
						neverFallback: "Never pulled"
					})
				}),
				/* @__PURE__ */ N($, {
					label: t("section.last_push", "Last push"),
					children: /* @__PURE__ */ N(Ve, {
						run: c.last_push,
						neverKey: "section.never_pushed",
						neverFallback: "Never pushed"
					})
				}),
				/* @__PURE__ */ N($, {
					label: t("section.unfinished_pulls", "Unfinished pulls"),
					children: u.length === 0 ? /* @__PURE__ */ N("span", {
						className: "text-muted-foreground",
						children: t("section.unfinished_none", "None")
					}) : /* @__PURE__ */ P("span", {
						className: "flex items-center gap-2",
						children: [/* @__PURE__ */ N(d, {
							name: "triangle-alert",
							size: 12,
							"aria-hidden": "true"
						}), t(u.length === 1 ? "section.unfinished_one" : "section.unfinished_other", u.length === 1 ? "{{count}} object type resumes on the next pull" : "{{count}} object types resume on the next pull", { count: u.length })]
					})
				}),
				/* @__PURE__ */ N($, {
					label: t("section.open_conflicts", "Open conflicts"),
					children: /* @__PURE__ */ N(Ge, {
						conflicts: c.open_conflicts,
						capped: c.open_conflicts_capped
					})
				})
			] })]
		})]
	});
}
function Je(e) {
	let t = e || [], n = Ee(t);
	return a({
		queryKey: [
			"plugin-jira",
			"links",
			"attribute",
			t.join(",")
		],
		queryFn: async () => {
			let e = [];
			for (let t of n) {
				let n = x({ ids: t }), r = await g.get(`plugins/jira/links/attribute?${n}`);
				e.push(...r.data?.items || []);
			}
			return e;
		},
		enabled: t.length > 0,
		retry: !1
	});
}
function Ye({ objectType: e }) {
	let { t } = E("jira"), n = e?.id, r = e?.schema_id, i = Ne("object_type", n ? [n] : []), a = T(n), o = a.data || [], s = Je(o.map((e) => e.id)), c = D(r), l = Ne("schema", r ? [r] : []), u = i.data?.[0] || null, d = !!u?.linked, f = l.data?.[0] || null, p = f?.linked ? f.jira_id : null, m = Ie(d ? ke(p, u.jira_id) : null, c.data?.source_id, d);
	if (!n) return null;
	if (i.isLoading || a.isLoading || s.isLoading) return /* @__PURE__ */ N(Re, {});
	if (i.isError || a.isError || s.isError) return /* @__PURE__ */ N(ze, {
		error: i.error || a.error || s.error,
		onRetry: () => {
			i.refetch(), a.refetch(), s.refetch();
		}
	});
	if (!d) return /* @__PURE__ */ N("p", {
		className: "text-sm text-muted-foreground",
		"data-testid": "plugin-jira-object-type-unlinked",
		children: t("section.unlinked_object_type", "This object type isn't linked to Jira.")
	});
	let h = new Map((s.data || []).map((e) => [e.id, e]));
	return /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": "plugin-jira-object-type-section",
		children: [/* @__PURE__ */ P(Be, { children: [/* @__PURE__ */ N($, {
			label: t("section.object_type_id_label", "Jira object type ID"),
			children: /* @__PURE__ */ N(Le, {
				jiraId: u.jira_id,
				href: m
			})
		}), /* @__PURE__ */ N($, {
			label: t("direction.label", "Sync direction"),
			children: /* @__PURE__ */ N(Ue, {
				entity: "object_type",
				entityId: n,
				item: u
			})
		})] }), /* @__PURE__ */ P("div", {
			className: "space-y-3",
			children: [/* @__PURE__ */ N("h3", {
				className: "text-sm font-semibold",
				children: t("section.heading_attributes", "Attributes")
			}), /* @__PURE__ */ N("div", {
				className: "rounded-lg border overflow-x-auto",
				children: /* @__PURE__ */ P("table", {
					className: "w-full text-sm",
					children: [/* @__PURE__ */ N("thead", { children: /* @__PURE__ */ P("tr", {
						className: "border-b text-left text-xs text-muted-foreground",
						children: [
							/* @__PURE__ */ N("th", {
								className: "px-4 py-3 font-medium",
								children: t("section.attribute_name", "Attribute")
							}),
							/* @__PURE__ */ N("th", {
								className: "px-4 py-3 font-medium",
								children: t("section.attribute_jira_id", "Jira ID")
							}),
							/* @__PURE__ */ N("th", {
								className: "px-4 py-3 font-medium",
								children: t("section.sync_direction_column", "Sync direction")
							}),
							/* @__PURE__ */ N("th", {
								className: "px-4 py-3 font-medium",
								children: t("section.effective_direction_column", "Effective direction")
							})
						]
					}) }), /* @__PURE__ */ N("tbody", { children: o.map((e) => {
						let n = h.get(e.id);
						return /* @__PURE__ */ P("tr", {
							className: "border-b last:border-0",
							"data-testid": `plugin-jira-attribute-${e.id}`,
							children: [
								/* @__PURE__ */ N("td", {
									className: "px-4 py-3",
									children: e.name
								}),
								/* @__PURE__ */ N("td", {
									className: "px-4 py-3 tabular-nums",
									children: n?.linked ? n.jira_id : /* @__PURE__ */ N("span", {
										className: "text-muted-foreground",
										children: t("section.not_in_jira", "Not in Jira")
									})
								}),
								/* @__PURE__ */ N("td", {
									className: "px-4 py-3",
									children: n?.linked ? /* @__PURE__ */ N(Ue, {
										entity: "attribute",
										entityId: e.id,
										item: n
									}) : /* @__PURE__ */ N("span", {
										className: "text-muted-foreground",
										children: "—"
									})
								}),
								/* @__PURE__ */ N("td", {
									className: "px-4 py-3 text-xs text-muted-foreground",
									children: n?.linked ? y(t("direction.effective", "Effective: {{direction}}."), { direction: t(`direction.${n.effective_direction}`, n.effective_direction || "") }) : ""
								})
							]
						}, e.id);
					}) })]
				})
			})]
		})]
	});
}
function Xe({ object: e }) {
	let { t } = E("jira"), n = C("sync", "trigger"), i = o(), [a, c] = r(!1), l = e?.id, u = Ne("object", l ? [l] : []), f = D(e?.schema_id), p = u.data?.[0] || null, m = !!p?.linked, h = Ie(m ? Ae(p.jira_id) : null, f.data?.source_id, m);
	if (!l) return null;
	if (u.isLoading) return /* @__PURE__ */ N(Re, {});
	if (u.isError) return /* @__PURE__ */ N(ze, {
		error: u.error,
		onRetry: () => u.refetch()
	});
	if (!m) return /* @__PURE__ */ N("p", {
		className: "text-sm text-muted-foreground",
		"data-testid": "plugin-jira-object-unlinked",
		children: t("section.unlinked_object", "This object isn't linked to Jira.")
	});
	let b = !p.is_detached, x = e?.name ?? "";
	return /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": "plugin-jira-object-section",
		children: [
			/* @__PURE__ */ P(Be, { children: [p.jira_key ? /* @__PURE__ */ N($, {
				label: t("section.object_label", "Jira object"),
				children: /* @__PURE__ */ P("span", {
					className: "flex flex-wrap items-baseline gap-2",
					children: [/* @__PURE__ */ N(Le, {
						jiraId: p.jira_key,
						href: h
					}), /* @__PURE__ */ N("span", {
						className: "text-xs text-muted-foreground tabular-nums",
						children: y(t("section.object_id_hint", "ID {{id}}"), { id: p.jira_id })
					})]
				})
			}) : /* @__PURE__ */ N($, {
				label: t("section.object_id_label", "Jira object ID"),
				children: /* @__PURE__ */ N(Le, {
					jiraId: p.jira_id,
					href: h
				})
			}), /* @__PURE__ */ N($, {
				label: t("section.sync_label", "Sync"),
				children: /* @__PURE__ */ P("span", {
					className: "flex items-center gap-2 text-sm",
					children: [/* @__PURE__ */ N(d, {
						name: b ? "refresh-cw" : "unlink",
						size: 14,
						"aria-hidden": "true"
					}), b ? t("section.object_syncing", "Syncing with Jira") : t("section.object_detached", "Detached from Jira")]
				})
			})] }),
			b && /* @__PURE__ */ P("p", {
				className: "flex items-start gap-2 text-xs text-muted-foreground",
				children: [/* @__PURE__ */ N(d, {
					name: "lock",
					size: 12,
					"aria-hidden": "true",
					className: "mt-0.5"
				}), t("section.object_lock_hint", "Values of pull-only attributes are locked here and change only through a Jira pull. Detach the object to edit them.")]
			}),
			n && /* @__PURE__ */ N("div", {
				className: "flex flex-wrap gap-2",
				children: /* @__PURE__ */ N(s, {
					type: "button",
					variant: "outline",
					size: "sm",
					leftIcon: b ? "unlink" : "link",
					loading: a,
					onClick: async () => {
						let e = b;
						if (await _({
							message: y(e ? t("section.detach_confirm", "Detach “{{name}}” from Jira? Its values stay as they are and become editable here. Pulls and pushes skip this object until you reattach it.") : t("section.reattach_confirm", "Reattach “{{name}}” to Jira? Pull-only values lock again, and the next pull replaces them with the values from Jira."), { name: x }),
							danger: !1
						})) {
							c(!0);
							try {
								await g.put(`plugins/jira/objects/${l}/detach`, { detached: e }), S(y(e ? t("section.detach_toast", "Detached “{{name}}” from Jira.") : t("section.reattach_toast", "Reattached “{{name}}” to Jira."), { name: x })), i.invalidateQueries({ queryKey: ["plugin-jira", "links"] }), i.invalidateQueries({ queryKey: ["object", l] }), i.invalidateQueries({ queryKey: ["plugin-item-badges"] });
							} catch (n) {
								let r = n?.response?.status === 409 ? t("section.detach_stale", "This object's Jira link is out of date. Run a pull for its Source, then try again.") : y(e ? t("section.detach_error", "Couldn't detach “{{name}}”: {{message}}.") : t("section.reattach_error", "Couldn't reattach “{{name}}”: {{message}}."), {
									name: x,
									message: v(n)
								});
								S(r, { type: "error" });
							} finally {
								c(!1);
							}
						}
					},
					"data-testid": "plugin-jira-object-toggle",
					children: b ? t("section.detach_button", "Detach from Jira") : t("section.reattach_button", "Reattach to Jira")
				})
			})
		]
	});
}
var Ze = [
	"pending",
	"resolved_keep_local",
	"resolved_keep_remote",
	"resolved_auto"
], Qe = {
	pending: "conflicts.status.pending",
	resolved_keep_local: "conflicts.status.resolved_keep_local",
	resolved_keep_remote: "conflicts.status.resolved_keep_remote",
	resolved_auto: "conflicts.status.resolved_auto"
}, $e = {
	integer: "value_integer",
	double: "value_float",
	float: "value_float",
	number: "value_float",
	boolean: "value_boolean",
	date: "value_datetime",
	datetime: "value_datetime",
	json: "value_struct",
	status: "value_struct"
};
function et(e) {
	return $e[e] || "value_simple";
}
function tt(e) {
	return e === "date" || e === "datetime";
}
function nt(e) {
	return !e || typeof e != "object" || e.v !== 1 || !Array.isArray(e.items) ? null : e;
}
function rt(e, t) {
	let n;
	try {
		n = JSON.parse(e);
	} catch {
		return String(e);
	}
	let r = et(t);
	if (r === "value_struct") return n;
	if (n && typeof n == "object" && !Array.isArray(n)) {
		if (Object.prototype.hasOwnProperty.call(n, r)) return n[r];
		let e = Object.keys(n);
		if (e.length === 1) return n[e[0]];
	}
	return n;
}
function it(e) {
	let t = nt(e);
	return t ? t.kind === "reference" ? t.items.map((e) => ({
		kind: "reference",
		objectId: e?.object_id || null,
		jiraObjectId: e?.jira_object_id || null,
		value: null
	})) : t.items.map((e) => ({
		kind: "scalar",
		objectId: null,
		jiraObjectId: null,
		value: rt(e, t.data_type)
	})) : [];
}
function at(e, t) {
	return e.kind === "reference" ? `ref:${e.objectId || ""}:${e.jiraObjectId || ""}` : `val:${t}:${JSON.stringify(e.value)}`;
}
function ot(e) {
	return it(e).filter((e) => e.objectId).map((e) => e.objectId);
}
function st(e, t) {
	let n = /* @__PURE__ */ new Map();
	return e.forEach((e) => {
		let r = at(e, t);
		n.set(r, (n.get(r) || 0) + 1);
	}), n;
}
function ct(e, t) {
	let n = it(e), r = it(t), i = st(n, e?.data_type), a = st(r, t?.data_type), o = (e, t, n, r) => e.map((e) => {
		let t = at(e, r);
		return {
			...e,
			key: t,
			shared: (n.get(t) || 0) > 0
		};
	}).sort((e, t) => Number(e.shared) - Number(t.shared)), s = 0;
	return i.forEach((e, t) => {
		(a.get(t) || 0) === 0 && (s += 1);
	}), a.forEach((e, t) => {
		(i.get(t) || 0) === 0 && (s += 1);
	}), {
		left: o(n, i, a, e?.data_type),
		right: o(r, a, i, t?.data_type),
		differCount: s
	};
}
function lt(e) {
	let t = new URLSearchParams(e || ""), n = t.get("status"), r = t.get("direction");
	return {
		status: Ze.includes(n) ? n : "pending",
		direction: r === "pull" || r === "push" ? r : null,
		schemaId: t.get("schema") || null
	};
}
function ut(e, t = "") {
	let n = new URLSearchParams(t || "");
	return e.status && e.status !== "pending" ? n.set("status", e.status) : n.delete("status"), e.direction ? n.set("direction", e.direction) : n.delete("direction"), e.schemaId ? n.set("schema", e.schemaId) : n.delete("schema"), n.toString();
}
function dt(e, t) {
	let n = {
		status: e.status || "pending",
		limit: 50,
		offset: t
	};
	return e.direction && (n.direction = e.direction), e.schemaId && (n.schema_id = e.schemaId), n;
}
function ft(e, t) {
	let n = /* @__PURE__ */ new Set(), r = [];
	return [...e || [], ...t || []].forEach((e) => {
		e && !n.has(e.id) && (n.add(e.id), r.push(e));
	}), r;
}
// ../plugins/jira/frontend/JiraConflictsPage.jsx
var pt = [
	"pending",
	"resolved_keep_local",
	"resolved_keep_remote",
	"resolved_auto"
];
async function mt(e, t) {
	let { data: n } = await g.get("plugins/jira/conflicts", { params: dt(e, t) });
	return n;
}
async function ht(e) {
	if (!e.length) return /* @__PURE__ */ new Map();
	let t = x({ ids: e }), { data: n } = await g.get(`plugins/jira/conflicts/object-names?${t}`), r = /* @__PURE__ */ new Map();
	return (n?.items || []).forEach((e) => r.set(e.id, e.name)), r;
}
function gt(e) {
	let n = t(() => [...new Set(ot(e.local).concat(ot(e.remote)))], [e]);
	return a({
		queryKey: [
			"jira-conflict-names",
			e.id,
			n
		],
		queryFn: () => ht(n),
		enabled: n.length > 0
	});
}
function _t(e, t, n, r) {
	return e == null || e === "" ? n("conflicts.no_value", "No value") : typeof e == "boolean" ? e ? n("conflicts.value.yes", "Yes") : n("conflicts.value.no", "No") : tt(t) ? r(e) : typeof e == "object" ? JSON.stringify(e) : String(e);
}
function vt(e, t) {
	let n = e?.response?.status, r = e?.response?.data?.detail;
	if (n === 409) {
		if (r?.error?.code === "CONFLICT_DIRECTION_NOT_BOTH") return t("conflicts.error.direction", "This attribute no longer syncs both ways, so the conflict can't be resolved here. Set its direction to Both directions first.");
		let e = typeof r == "string" ? r : JSON.stringify(r || "");
		return /link|binding/i.test(e) ? t("conflicts.error.no_link", "This object isn't linked to Jira anymore, or its link is out of date. Run a pull for its Source, then review the conflict again.") : t("conflicts.error.stale_version", "Someone else changed this conflict. The list is refreshed, so review it again.");
	}
	return y(n === 502 ? t("conflicts.error.jira", "Jira didn't accept the change: {{message}}. The conflict stays open.") : t("conflicts.error.generic", "The conflict could not be resolved: {{message}}."), { message: v(e) });
}
function yt({ item: e, side: t, names: n, dataType: r, t: i, formatDate: a }) {
	let o = y(i("conflicts.reference_fallback", "Jira object {{id}}"), { id: e.jiraObjectId || e.objectId || "" }), s = e.kind === "reference" ? e.objectId && n.get(e.objectId) || o : _t(e.value, r, i, a), c = t === "left" ? i("conflicts.item.not_in_jira", "Not in Jira") : i("conflicts.item.not_in_indramesh", "Not in IndraMesh");
	return /* @__PURE__ */ P("li", {
		className: "space-y-0.5",
		children: [e.shared ? /* @__PURE__ */ N("span", {
			className: "whitespace-pre-wrap break-words text-muted-foreground",
			children: s
		}) : /* @__PURE__ */ N("span", {
			className: "inline-block whitespace-pre-wrap break-words rounded-sm bg-muted px-2 py-1 text-foreground",
			children: s
		}), !e.shared && /* @__PURE__ */ N("p", {
			className: "text-xs text-muted-foreground",
			children: c
		})]
	});
}
function bt({ conflict: n, canManage: a, onResolved: c, t: l, formatDate: u }) {
	let d = gt(n).data || /* @__PURE__ */ new Map(), f = t(() => ct(n.local, n.remote), [n]), m = (k(n.source_id, 0, 50, n.target_id).data || []).map((e) => ({
		value: e.id,
		label: e.name || e.username || e.id
	})), [h, _] = r(m.length === 1 ? m[0].value : ""), v = m.length === 1 ? m[0].value : "";
	e(() => {
		!h && v && _(v);
	}, [h, v]);
	let [b, x] = r(null), [C, w] = r(null), T = o(), E = i({
		mutationFn: (e) => g.post(`plugins/jira/conflicts/${n.id}/resolve`, {
			resolution: e,
			version: n.version,
			service_account_id: h
		}),
		onSuccess: (e, t) => {
			T.invalidateQueries({ queryKey: ["jira-conflicts"] }), T.invalidateQueries({ queryKey: ["jira-conflict-count"] }), T.invalidateQueries({ queryKey: ["jira-conflicts-schemas"] }), S(y(t === "keep_local" ? l("conflicts.toast.keep_local", "Kept IndraMesh values for {{attribute}} on “{{object}}”.") : l("conflicts.toast.keep_remote", "Kept Jira values for {{attribute}} on “{{object}}”."), {
				attribute: n.attribute_name,
				object: n.object_name
			})), c();
		},
		onError: (e) => {
			let t = vt(e, l);
			e?.response?.status === 409 ? (T.invalidateQueries({ queryKey: ["jira-conflicts"] }), T.invalidateQueries({ queryKey: ["jira-conflict-count"] }), S(t), c()) : w(t);
		},
		onSettled: () => x(null)
	}), D = n.local?.cardinality === "single" ? l("conflicts.detail.summary_plain", "IndraMesh and Jira have different values.") : f.differCount === 1 ? y(l("conflicts.detail.summary_one", "{{count}} value differs between IndraMesh and Jira."), { count: 1 }) : y(l("conflicts.detail.summary_other", "{{count}} values differ between IndraMesh and Jira."), { count: f.differCount }), O = [{
		side: "left",
		title: l("conflicts.detail.left", "In IndraMesh ({{count}})"),
		items: f.left,
		dataType: n.local?.data_type
	}, {
		side: "right",
		title: l("conflicts.detail.right", "In Jira ({{count}})"),
		items: f.right,
		dataType: n.remote?.data_type
	}], A = {
		resolved_keep_local: "conflicts.resolved.keep_local",
		resolved_keep_remote: "conflicts.resolved.keep_remote",
		resolved_auto: "conflicts.resolved.auto"
	}[n.status];
	return /* @__PURE__ */ P("div", {
		className: "space-y-4",
		"data-testid": `jira-conflicts-detail-${n.id}`,
		children: [
			/* @__PURE__ */ N("p", {
				className: "text-sm",
				children: D
			}),
			/* @__PURE__ */ N("div", {
				className: "grid gap-4 sm:grid-cols-2",
				children: O.map((e) => /* @__PURE__ */ P("div", { children: [/* @__PURE__ */ N("h2", {
					className: "text-sm font-semibold",
					children: y(e.title, { count: e.items.length })
				}), e.items.length === 0 ? /* @__PURE__ */ N("p", {
					className: "mt-2 text-sm text-muted-foreground",
					children: l("conflicts.no_value", "No value")
				}) : /* @__PURE__ */ N("ul", {
					className: "mt-2 max-h-80 space-y-2 overflow-y-auto",
					children: e.items.map((t, n) => /* @__PURE__ */ N(yt, {
						item: t,
						side: e.side,
						names: d,
						dataType: e.dataType,
						t: l,
						formatDate: u
					}, `${t.key}-${n}`))
				})] }, e.side))
			}),
			n.status === "pending" && a && /* @__PURE__ */ P("div", {
				className: "space-y-2",
				children: [
					/* @__PURE__ */ N(p, {
						id: `jira-conflicts-account-${n.id}`,
						"data-testid": `jira-conflicts-account-${n.id}`,
						"aria-label": l("conflicts.account.label", "Service account"),
						value: h,
						disabled: m.length === 0,
						placeholder: l("conflicts.account.placeholder", "Select a service account…"),
						options: m,
						onChange: (e) => _(e.target.value)
					}),
					/* @__PURE__ */ N("p", {
						className: "text-xs text-muted-foreground",
						children: m.length === 0 ? y(l("conflicts.account.none_help", "This conflict has no eligible service account yet. Add one under Service accounts."), {}) : l("conflicts.account.help", "Used to write the kept values.")
					}),
					m.length === 0 && /* @__PURE__ */ N(F, {
						to: "/configuration/service-accounts",
						className: "text-xs text-primary underline-offset-4 hover:underline",
						children: l("conflicts.account.manage", "Manage service accounts")
					}),
					/* @__PURE__ */ P("div", {
						className: "flex flex-wrap gap-2",
						children: [/* @__PURE__ */ N(s, {
							variant: "outline",
							size: "sm",
							"data-testid": `jira-conflicts-keep-local-${n.id}`,
							disabled: !h || E.isPending,
							loading: b === "keep_local",
							onClick: () => {
								w(null), x("keep_local"), E.mutate("keep_local");
							},
							children: l("conflicts.action.keep_local", "Keep IndraMesh values")
						}), /* @__PURE__ */ N(s, {
							variant: "outline",
							size: "sm",
							"data-testid": `jira-conflicts-keep-remote-${n.id}`,
							disabled: !h || E.isPending,
							loading: b === "keep_remote",
							onClick: () => {
								w(null), x("keep_remote"), E.mutate("keep_remote");
							},
							children: l("conflicts.action.keep_remote", "Keep Jira values")
						})]
					}),
					/* @__PURE__ */ N("p", {
						className: "text-xs text-muted-foreground",
						children: l("conflicts.action.consequence", "Keep IndraMesh values writes these values to Jira. Keep Jira values replaces them in IndraMesh.")
					}),
					C && /* @__PURE__ */ N("p", {
						className: "text-xs text-destructive",
						role: "alert",
						children: C
					})
				]
			}),
			n.status !== "pending" && A && /* @__PURE__ */ N("p", {
				className: "text-xs text-muted-foreground",
				children: y(l(A, "Resolved on {{date}}."), { date: u(n.resolved_at) })
			})
		]
	});
}
function xt({ conflict: e, open: t, onToggle: n, onResolved: r, names: i, canManage: a, t: o, formatDate: c }) {
	let l = e.status === "pending";
	return /* @__PURE__ */ P(M, { children: [/* @__PURE__ */ P("tr", {
		"data-testid": `jira-conflicts-row-${e.id}`,
		children: [
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-sm font-semibold",
				children: e.object_name
			}),
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-sm text-muted-foreground",
				children: e.attribute_name
			}),
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-sm text-muted-foreground",
				children: i.get(e.schema_id) || ""
			}),
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-sm",
				children: e.direction === "pull" ? o("conflicts.direction.pull", "Pull") : o("conflicts.direction.push", "Push")
			}),
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-sm",
				children: c(e.updated_at)
			}),
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-sm",
				children: o(Qe[e.status], e.status)
			}),
			/* @__PURE__ */ N("td", {
				className: "px-4 py-3 text-right",
				children: /* @__PURE__ */ N(s, {
					variant: "outline",
					size: "sm",
					"aria-expanded": t,
					"aria-controls": `jira-conflicts-detail-${e.id}`,
					"aria-label": y(l ? o("conflicts.row.review_aria", "Review conflict for {{attribute}} on “{{object}}”") : o("conflicts.row.details_aria", "Show details for {{attribute}} on “{{object}}”"), {
						attribute: e.attribute_name,
						object: e.object_name
					}),
					"data-testid": `jira-conflicts-review-${e.id}`,
					onClick: n,
					children: l ? o("conflicts.row.review", "Review") : o("conflicts.row.details", "Details")
				})
			})
		]
	}), t && /* @__PURE__ */ N("tr", { children: /* @__PURE__ */ N("td", {
		colSpan: 7,
		id: `jira-conflicts-detail-${e.id}`,
		className: "border-l-2 border-primary bg-muted/20 px-4 py-4",
		onKeyDown: (e) => {
			e.key === "Escape" && (e.stopPropagation(), n());
		},
		children: /* @__PURE__ */ N(bt, {
			conflict: e,
			canManage: a,
			onResolved: r,
			t: o,
			formatDate: c
		})
	}) })] });
}
function St() {
	let { data: e } = a({
		queryKey: ["jira-conflict-count"],
		queryFn: async () => (await g.get("plugins/jira/conflicts/count")).data,
		staleTime: 3e4
	});
	return e?.pending ?? 0;
}
function Ct(e) {
	return e >= 100 ? b("jira", "conflicts.nav.badge_overflow", { defaultValue: "More than 99 open Jira conflicts" }) : b("jira", e === 1 ? "conflicts.nav.badge_one" : "conflicts.nav.badge_other", {
		count: e,
		defaultValue: e === 1 ? "{{count}} open Jira conflict" : "{{count}} open Jira conflicts"
	});
}
function wt(e) {
	typeof document < "u" && requestAnimationFrame(() => {
		document.querySelector(`[data-testid="jira-conflicts-review-${e}"]`)?.focus?.();
	});
}
function Tt() {
	let { t: e } = E("jira"), n = w(), i = C("conflicts", "read"), o = C("conflicts", "manage"), [c, u] = I(), [d, g] = r(null), [_, b] = r(0), [x, S] = r([]), T = t(() => lt(c.toString()), [c]), D = T.status !== "pending" || !!T.direction || !!T.schemaId, k = O(null, 0, 200, { enabled: i }), A = t(() => {
		let e = /* @__PURE__ */ new Map();
		return (k.data?.items || k.data || []).forEach((t) => e.set(t.id, t.name)), e;
	}, [k.data]), j = a({
		queryKey: [
			"jira-conflicts",
			T,
			_
		],
		queryFn: () => mt(T, _),
		enabled: i,
		placeholderData: (e) => e
	}), M = t(() => ft(x, j.data?.items), [x, j.data]), F = (e) => {
		g(null), b(0), S([]), u(ut({
			...T,
			...e
		}), { replace: !0 });
	}, L = () => g(null), R = (e) => {
		let t = M.findIndex((t) => t.id === e), n = t >= 0 ? M[t + 1] : null;
		g(null), n ? wt(n.id) : typeof document < "u" && document.querySelector("[data-testid=\"jira-conflicts-page\"]")?.focus?.();
	};
	if (!i) return /* @__PURE__ */ P("div", {
		className: "space-y-6",
		"data-testid": "jira-conflicts-page",
		children: [/* @__PURE__ */ N(f, {
			title: e("conflicts.page.title", "Jira conflicts"),
			icon: "git-compare"
		}), /* @__PURE__ */ N(l, {
			variant: "error",
			"data-testid": "jira-conflicts-no-access",
			title: e("conflicts.page.no_access_title", "You don't have access to Jira conflicts"),
			description: e("conflicts.page.no_access", "Ask an administrator for the Conflicts read permission.")
		})]
	});
	let z = j.isPending && M.length === 0;
	return /* @__PURE__ */ P("div", {
		className: "space-y-6",
		tabIndex: -1,
		"data-testid": "jira-conflicts-page",
		children: [
			/* @__PURE__ */ N(f, {
				title: e("conflicts.page.title", "Jira conflicts"),
				description: e("conflicts.page.description", "Values that changed in both IndraMesh and Jira since the last sync. Compare both sides, then choose which values to keep."),
				icon: "git-compare"
			}),
			/* @__PURE__ */ P("div", {
				className: "flex flex-wrap items-center gap-3",
				children: [
					/* @__PURE__ */ P("label", {
						className: "flex items-center gap-2 text-sm",
						children: [/* @__PURE__ */ N("span", {
							className: "text-muted-foreground",
							children: e("conflicts.filter.schema", "Schema")
						}), /* @__PURE__ */ N(p, {
							"aria-label": e("conflicts.filter.schema", "Schema"),
							"data-testid": "jira-conflicts-filter-schema",
							value: T.schemaId || "",
							options: [{
								value: "",
								label: e("conflicts.filter.schema_all", "All schemas")
							}, ...(k.data?.items || k.data || []).map((e) => ({
								value: e.id,
								label: e.name
							}))],
							onChange: (e) => F({ schemaId: e.target.value || null })
						})]
					}),
					/* @__PURE__ */ P("label", {
						className: "flex items-center gap-2 text-sm",
						children: [/* @__PURE__ */ N("span", {
							className: "text-muted-foreground",
							children: e("conflicts.filter.direction", "Found during")
						}), /* @__PURE__ */ N(p, {
							"aria-label": e("conflicts.filter.direction", "Found during"),
							"data-testid": "jira-conflicts-filter-direction",
							value: T.direction || "",
							options: [
								{
									value: "",
									label: e("conflicts.filter.direction_all", "Pulls and pushes")
								},
								{
									value: "pull",
									label: e("conflicts.filter.direction_pull", "Pulls")
								},
								{
									value: "push",
									label: e("conflicts.filter.direction_push", "Pushes")
								}
							],
							onChange: (e) => F({ direction: e.target.value || null })
						})]
					}),
					/* @__PURE__ */ P("label", {
						className: "flex items-center gap-2 text-sm",
						children: [/* @__PURE__ */ N("span", {
							className: "text-muted-foreground",
							children: e("conflicts.filter.status", "Status")
						}), /* @__PURE__ */ N(p, {
							"aria-label": e("conflicts.filter.status", "Status"),
							"data-testid": "jira-conflicts-filter-status",
							value: T.status,
							options: pt.map((t) => ({
								value: t,
								label: e(Qe[t], t)
							})),
							onChange: (e) => F({ status: e.target.value })
						})]
					})
				]
			}),
			z ? /* @__PURE__ */ N(h, {
				rows: 5,
				cols: 7
			}) : j.isError ? /* @__PURE__ */ N(l, {
				variant: "error",
				"data-testid": "jira-conflicts-error",
				title: e("conflicts.page.load_error_title", "Couldn't load Jira conflicts"),
				description: y(e("conflicts.page.load_error", "Couldn't load Jira conflicts: {{message}}. Reload the page to try again."), { message: v(j.error) })
			}) : M.length === 0 ? D ? /* @__PURE__ */ N(l, {
				variant: "no-results",
				"data-testid": "jira-conflicts-no-results",
				title: e("conflicts.page.no_results_title", "No conflicts match these filters"),
				description: e("conflicts.page.no_results_body", "Change the schema, direction or status filter to see other conflicts.")
			}) : /* @__PURE__ */ N(l, {
				variant: "empty",
				"data-testid": "jira-conflicts-empty",
				title: e("conflicts.page.empty_title", "No open Jira conflicts"),
				description: e("conflicts.page.empty_body", "When a value changes in both IndraMesh and Jira between syncs, the conflict appears here for you to decide.")
			}) : /* @__PURE__ */ P(m, {
				"data-testid": "jira-conflicts-table",
				children: [/* @__PURE__ */ N("thead", { children: /* @__PURE__ */ P("tr", {
					className: "border-b",
					children: [
						/* @__PURE__ */ N("th", {
							className: "text-left px-4 py-2 text-xs font-semibold",
							children: e("conflicts.column.object", "Object")
						}),
						/* @__PURE__ */ N("th", {
							className: "text-left px-4 py-2 text-xs font-semibold",
							children: e("conflicts.column.attribute", "Attribute")
						}),
						/* @__PURE__ */ N("th", {
							className: "text-left px-4 py-2 text-xs font-semibold",
							children: e("conflicts.column.schema", "Schema")
						}),
						/* @__PURE__ */ N("th", {
							className: "text-left px-4 py-2 text-xs font-semibold",
							children: e("conflicts.column.direction", "Found during")
						}),
						/* @__PURE__ */ N("th", {
							className: "text-left px-4 py-2 text-xs font-semibold",
							children: e("conflicts.column.changed", "Last changed")
						}),
						/* @__PURE__ */ N("th", {
							className: "text-left px-4 py-2 text-xs font-semibold",
							children: e("conflicts.column.status", "Status")
						}),
						/* @__PURE__ */ N("th", {
							className: "px-4 py-2 text-right text-xs font-semibold",
							children: /* @__PURE__ */ N("span", {
								className: "sr-only",
								children: e("conflicts.column.actions", "Actions")
							})
						})
					]
				}) }), /* @__PURE__ */ N("tbody", { children: M.map((t) => /* @__PURE__ */ N(xt, {
					conflict: t,
					open: d === t.id,
					onToggle: () => {
						d === t.id ? (L(), wt(t.id)) : g(t.id);
					},
					onResolved: () => R(t.id),
					names: A,
					canManage: o,
					t: e,
					formatDate: n
				}, t.id)) })]
			}),
			!z && j.data?.next_offset !== null && j.data?.next_offset !== void 0 && /* @__PURE__ */ N(s, {
				variant: "outline",
				size: "sm",
				"data-testid": "jira-conflicts-load-more",
				loading: j.isFetching && _ > 0,
				onClick: () => {
					S(M), b(j.data.next_offset);
				},
				children: e("conflicts.load_more", "Load more conflicts")
			})
		]
	});
}
// ../plugins/jira/frontend/badge-logic.js
var Et = "jira";
function Dt(e) {
	return e === Et;
}
var Ot = {
	label: "Jira",
	labelKey: "badge.media.label",
	title: "Imported from Jira. Read-only here.",
	titleKey: "badge.media.title",
	icon: "link"
};
function kt(e) {
	return Dt(e) ? { ...Ot } : null;
}
function At({ ids: e, context: t } = {}) {
	let n = t?.itemsById || {}, r = /* @__PURE__ */ new Map();
	for (let t of e || []) {
		let e = kt(n[t]);
		e && r.set(t, [e]);
	}
	return r;
}
function jt(e, t = 100) {
	return Ee((e || []).filter(Boolean), t);
}
function Mt(e) {
	if (!e?.linked || !e.jira_id) return null;
	let t = String(e.jira_id), n = e.jira_key ? String(e.jira_key) : null, r = n ? `${n} (id ${t})` : t;
	return e.is_detached ? {
		label: "Detached",
		labelKey: "badge.object.detached.label",
		title: `Detached from Jira object ${r}. Syncs skip this object.`,
		icon: "unlink"
	} : {
		label: "Jira",
		labelKey: "badge.object.linked.label",
		title: `Linked to Jira object ${r}.`,
		icon: "link"
	};
}
function Nt(e) {
	return !e?.linked || e.effective_direction !== "pull" ? null : {
		label: "Pull only",
		labelKey: "badge.attribute.pull_only.label",
		title: "Changes only through a Jira pull. Detach the object to edit it here.",
		titleKey: "badge.attribute.pull_only.title"
	};
}
// ../plugins/jira/frontend/JiraBadges.jsx
async function Pt(e, t) {
	let n = [];
	for (let r of jt(t)) {
		let t = x({ ids: r }), i = await g.get(`plugins/jira/links/${e}?${t}`);
		n.push(...i.data?.items || []);
	}
	return n;
}
async function Ft({ ids: e }) {
	let t = await Pt("object", e), n = /* @__PURE__ */ new Map();
	for (let e of t) {
		let t = Mt(e);
		t && n.set(e.id, [t]);
	}
	return n;
}
async function It({ ids: e, context: t }) {
	let n = t?.objectId;
	if (!n) return /* @__PURE__ */ new Map();
	let r = (await Pt("object", [n])).find((e) => e.id === n);
	if (!r?.linked || r.is_detached) return /* @__PURE__ */ new Map();
	let i = await Pt("attribute", e), a = /* @__PURE__ */ new Map();
	for (let e of i) {
		let t = Nt(e);
		t && a.set(e.id, [t]);
	}
	return a;
}
// ../plugins/jira/frontend/index.jsx
var Lt = {
	translations: {
		en: {
			"task.source": "Source",
			"task.source_placeholder": "Select a Jira source…",
			"task.service_account": "Service account",
			"task.service_account_placeholder": "Select a service account…",
			"task.schema": "Schema",
			"task.schema_placeholder": "Select a schema…",
			"task.target": "Target",
			"task.target_placeholder": "Select a Target…",
			"task.jira_schema": "Jira schema",
			"task.jira_schema_all": "All linked Jira schemas in this Source",
			"task.connection_missing": "This Source has no Jira plugin connection. Configure it under Sources before starting a pull.",
			"task.connection_check_failed": "The Jira plugin connection could not be verified. Pull stays blocked until the connection can be checked.",
			"task.core_target_linked": "Core target — linked: {{schema}}",
			"task.core_target_new": "Core target — a new schema will be created.",
			"task.core_targets_all": "Core targets — linked: {{schemas}}",
			"task.core_target_none": "No linked Core schema. Select a Jira schema to create a new Core schema.",
			"task.jira_schema_help": "Pick one schema to pull only that schema.",
			"task.jira_schema_loading": "Loading Jira schemas…",
			"task.jira_schema_load_failed": "Couldn't load the Jira schemas: {{message}}. The pull can still run for all schemas.",
			"task.include_comments": "Include comments and attachments",
			"task.include_comments_help": "Also imports Jira comments and attachments. The pull takes longer because each object needs extra Jira requests.",
			"task.destination": "Destination Jira schema",
			"task.destination_placeholder": "Select a destination…",
			"task.destination_load_failed": "Couldn't load the Jira schemas for this Target: {{message}}. Check the service account, then choose the Target again.",
			"task.precheck_checking": "Checking whether the destination is empty…",
			"task.precheck_empty": "The destination is empty. Ready to export.",
			"task.precheck_not_empty_one": "The destination already has {{count}} object type. Export needs an empty Jira schema, so pick an empty one or push instead.",
			"task.precheck_not_empty_other": "The destination already has {{count}} object types. Export needs an empty Jira schema, so pick an empty one or push instead.",
			"task.precheck_failed": "Couldn't check the destination: {{message}}. Export stays blocked until the check succeeds.",
			"task.precheck_retry": "Check again",
			"task.no_sources": "There are no Jira sources yet. Add one under Sources.",
			"task.no_targets": "There are no Jira targets yet. Add one under Targets.",
			"task.no_service_accounts": "This {{kind}} has no eligible service account yet. Add one under Service accounts.",
			"task.action_open_sources": "Open Sources",
			"task.action_open_targets": "Open Targets",
			"task.action_open_service_accounts": "Open service accounts",
			"connector.strategy_label": "Default conflict handling",
			"connector.strategy_manual": "Review manually",
			"connector.strategy_prefer_local": "Prefer IndraMesh values",
			"connector.strategy_prefer_remote": "Prefer Jira values",
			"connector.role_help_source": "Used when pulling from Jira.",
			"connector.role_help_target": "Used when pushing to Jira.",
			"connector.scope_help": "Applies to conflicts found from now on. Conflicts that are already open keep their state.",
			"connector.stale_state": "The address changed after this connection was bound, so Jira synchronisation is locked for this record. Re-binding is not available yet.",
			"connector.url_changed_warning": "You changed the address. Saving it locks Jira synchronisation for this record because re-binding is not available yet.",
			"connector.load_failed": "Couldn't load the Jira connection settings. Reopen the dialog to try again.",
			"connector.bind_invalid_url": "The {{kind}} needs a valid http(s) address before the Jira connection can be saved.",
			"connector.bind_not_jira": "This {{kind}} is no longer a Jira {{kind}}, so no Jira connection can be saved.",
			"connector.bind_failed": "The Jira connection could not be saved. Check the address and try again.",
			"connector.account_help": "Saved with the connector settings and preselected for the connection test and Jira tasks.",
			"connector.test_button": "Test connection",
			"connector.tested_url_hint": "Tests the saved address {{url}}.",
			"connector.unsaved_url_hint": "Save first to test the address you just entered.",
			"connector.new_record_test_disabled": "Save this {{kind}} first. The connection test is available afterwards: assign a service account to this {{kind}} and test here.",
			"connector.no_account_test_disabled": "Add a service account for this {{kind}} to test the connection.",
			"connector.manage_link": "Manage service accounts",
			"connector.result_ok": "Connected. Jira accepted the service account.",
			"connector.result_auth": "Jira rejected the service account's credentials. Update its token under Service accounts, then test again.",
			"connector.result_permission": "Jira accepted the sign-in but the account can't read Assets schemas. Grant it Assets access in Jira, then test again.",
			"connector.result_network": "Jira didn't respond at {{url}}. Check the address and that Jira is reachable from IndraMesh.",
			"connector.result_not_found": "Jira has no schema with this ID. Check the schema, then test again.",
			"connector.result_error": "The connection test failed. Check the address and service account, then test again.",
			"section.schema.title": "Jira sync",
			"section.heading_link_direction": "Link and direction",
			"section.heading_status": "Status",
			"section.heading_attributes": "Attributes",
			"section.schema_id_label": "Jira schema",
			"section.object_type_id_label": "Jira object type ID",
			"section.object_label": "Jira object",
			"section.object_id_hint": "ID {{id}}",
			"section.object_id_label": "Jira object ID",
			"section.sync_label": "Sync",
			"section.attribute_jira_id": "Jira ID",
			"section.attribute_name": "Attribute",
			"section.sync_direction_column": "Sync direction",
			"section.effective_direction_column": "Effective direction",
			"section.not_in_jira": "Not in Jira",
			"section.unlinked_schema": "This schema isn't linked to Jira. Run a Jira pull for its Source to link it.",
			"section.unlinked_object_type": "This object type isn't linked to Jira.",
			"section.unlinked_object": "This object isn't linked to Jira.",
			"section.stale": "Out of date: the Source now points to a different Jira address. Run a pull to relink this schema.",
			"section.last_pull": "Last pull",
			"section.last_push": "Last push",
			"section.run_completed": "Completed {{date}}",
			"section.run_failed": "Failed {{date}}",
			"section.run_cancelled": "Cancelled {{date}}",
			"section.run_running": "Running since {{date}}",
			"section.run_queued": "Queued",
			"section.never_pulled": "Never pulled",
			"section.never_pushed": "Never pushed",
			"section.unfinished_pulls": "Unfinished pulls",
			"section.unfinished_none": "None",
			"section.unfinished_one": "{{count}} object type resumes on the next pull",
			"section.unfinished_other": "{{count}} object types resume on the next pull",
			"section.open_conflicts": "Open conflicts",
			"section.conflicts_none": "None",
			"section.conflicts_summary": "{{pull}} from pulls, {{push}} from pushes",
			"section.review_conflicts": "Review conflicts",
			"section.loading": "Loading Jira details…",
			"section.load_error": "Couldn't load the Jira details: {{message}}.",
			"section.retry": "Try again",
			"section.object_syncing": "Syncing with Jira",
			"section.object_detached": "Detached from Jira",
			"section.object_lock_hint": "Values of pull-only attributes are locked here and change only through a Jira pull. Detach the object to edit them.",
			"section.detach_button": "Detach from Jira",
			"section.reattach_button": "Reattach to Jira",
			"section.detach_confirm": "Detach “{{name}}” from Jira? Its values stay as they are and become editable here. Pulls and pushes skip this object until you reattach it.",
			"section.reattach_confirm": "Reattach “{{name}}” to Jira? Pull-only values lock again, and the next pull replaces them with the values from Jira.",
			"section.detach_toast": "Detached “{{name}}” from Jira.",
			"section.reattach_toast": "Reattached “{{name}}” to Jira.",
			"section.detach_error": "Couldn't detach “{{name}}”: {{message}}.",
			"section.reattach_error": "Couldn't reattach “{{name}}”: {{message}}.",
			"section.detach_stale": "This object's Jira link is out of date. Run a pull for its Source, then try again.",
			"direction.label": "Sync direction",
			"direction.inherit": "Inherit",
			"direction.default_both": "Default (both directions)",
			"direction.both": "Both directions",
			"direction.pull": "Pull from Jira only",
			"direction.push": "Push to Jira only",
			"direction.none": "Don't sync",
			"direction.effective": "Effective: {{direction}}.",
			"direction.origin_attribute": "Set here.",
			"direction.origin_object_type": "Inherited from the object type.",
			"direction.origin_schema": "Inherited from the schema.",
			"direction.origin_default": "Default because no level sets a direction.",
			"direction.lock_hint": "Locked in IndraMesh: values change only through a Jira pull.",
			"direction.saving": "Saving…",
			"direction.saved": "Saved",
			"direction.save_error": "Couldn't save the direction: {{message}}. The previous setting still applies.",
			"link.open_in_jira": "Open {{id}} in Jira (opens in a new tab)",
			"badge.media.label": "Jira",
			"badge.media.title": "Imported from Jira. Read-only here.",
			"badge.object.linked.label": "Jira",
			"badge.object.detached.label": "Detached",
			"badge.attribute.pull_only.label": "Pull only",
			"badge.attribute.pull_only.title": "Changes only through a Jira pull. Detach the object to edit it here.",
			"conflicts.nav.label": "Jira conflicts",
			"conflicts.nav.badge_one": "{{count}} open Jira conflict",
			"conflicts.nav.badge_other": "{{count}} open Jira conflicts",
			"conflicts.nav.badge_overflow": "More than 99 open Jira conflicts",
			"conflicts.page.title": "Jira conflicts",
			"conflicts.page.description": "Values that changed in both IndraMesh and Jira since the last sync. Compare both sides, then choose which values to keep.",
			"conflicts.page.empty_title": "No open Jira conflicts",
			"conflicts.page.empty_body": "When a value changes in both IndraMesh and Jira between syncs, the conflict appears here for you to decide.",
			"conflicts.page.no_results_title": "No conflicts match these filters",
			"conflicts.page.no_results_body": "Change the schema, direction or status filter to see other conflicts.",
			"conflicts.page.load_error_title": "Couldn't load Jira conflicts",
			"conflicts.page.load_error": "Couldn't load Jira conflicts: {{message}}. Reload the page to try again.",
			"conflicts.page.no_access_title": "You don't have access to Jira conflicts",
			"conflicts.page.no_access": "Ask an administrator for the Conflicts read permission.",
			"conflicts.filter.schema": "Schema",
			"conflicts.filter.schema_all": "All schemas",
			"conflicts.filter.direction": "Found during",
			"conflicts.filter.direction_all": "Pulls and pushes",
			"conflicts.filter.direction_pull": "Pulls",
			"conflicts.filter.direction_push": "Pushes",
			"conflicts.filter.status": "Status",
			"conflicts.column.object": "Object",
			"conflicts.column.attribute": "Attribute",
			"conflicts.column.schema": "Schema",
			"conflicts.column.direction": "Found during",
			"conflicts.column.changed": "Last changed",
			"conflicts.column.status": "Status",
			"conflicts.column.actions": "Actions",
			"conflicts.direction.pull": "Pull",
			"conflicts.direction.push": "Push",
			"conflicts.status.pending": "Open",
			"conflicts.status.resolved_keep_local": "Kept IndraMesh values",
			"conflicts.status.resolved_keep_remote": "Kept Jira values",
			"conflicts.status.resolved_auto": "Resolved automatically",
			"conflicts.row.review": "Review",
			"conflicts.row.details": "Details",
			"conflicts.row.review_aria": "Review conflict for {{attribute}} on “{{object}}”",
			"conflicts.row.details_aria": "Show details for {{attribute}} on “{{object}}”",
			"conflicts.detail.summary_one": "{{count}} value differs between IndraMesh and Jira.",
			"conflicts.detail.summary_other": "{{count}} values differ between IndraMesh and Jira.",
			"conflicts.detail.summary_plain": "IndraMesh and Jira have different values.",
			"conflicts.detail.left": "In IndraMesh ({{count}})",
			"conflicts.detail.right": "In Jira ({{count}})",
			"conflicts.item.not_in_jira": "Not in Jira",
			"conflicts.item.not_in_indramesh": "Not in IndraMesh",
			"conflicts.no_value": "No value",
			"conflicts.value.yes": "Yes",
			"conflicts.value.no": "No",
			"conflicts.reference_fallback": "Jira object {{id}}",
			"conflicts.account.label": "Service account",
			"conflicts.account.placeholder": "Select a service account…",
			"conflicts.account.help": "Used to write the kept values.",
			"conflicts.account.none_help": "This conflict has no eligible service account yet. Add one under Service accounts.",
			"conflicts.account.manage": "Manage service accounts",
			"conflicts.action.keep_local": "Keep IndraMesh values",
			"conflicts.action.keep_remote": "Keep Jira values",
			"conflicts.action.consequence": "Keep IndraMesh values writes these values to Jira. Keep Jira values replaces them in IndraMesh.",
			"conflicts.resolved.keep_local": "Kept IndraMesh values on {{date}}.",
			"conflicts.resolved.keep_remote": "Kept Jira values on {{date}}.",
			"conflicts.resolved.auto": "Resolved automatically on {{date}}.",
			"conflicts.toast.keep_local": "Kept IndraMesh values for {{attribute}} on “{{object}}”.",
			"conflicts.toast.keep_remote": "Kept Jira values for {{attribute}} on “{{object}}”.",
			"conflicts.error.stale_version": "Someone else changed this conflict. The list is refreshed, so review it again.",
			"conflicts.error.no_link": "This object isn't linked to Jira anymore, or its link is out of date. Run a pull for its Source, then review the conflict again.",
			"conflicts.error.direction": "This attribute no longer syncs both ways, so the conflict can't be resolved here. Set its direction to Both directions first.",
			"conflicts.error.jira": "Jira didn't accept the change: {{message}}. The conflict stays open.",
			"conflicts.error.generic": "The conflict could not be resolved: {{message}}.",
			"conflicts.load_more": "Load more conflicts",
			"task.target_schema": "Core schema",
			"task.target_schema_help": "Pull into an existing empty schema of this Source to keep its name and key prefix, or create a new schema named after the Jira schema.",
			"task.target_schema_new": "Create a new schema",
			"task.core_target_adopt": "Core target — the existing schema {{schema}} will be linked.",
			"task.target_schema_none": "This Source has no empty schema to pull into. To choose the name and key prefix yourself, create an empty schema for this Source first.",
			"section.link_requires_empty": "Only an empty schema (without object types) can be linked to an existing Jira schema here.",
			"section.link_toast": "Linked to Jira. The next pull for this Source fills this schema.",
			"section.link_error": "Couldn't link this schema: {{message}}.",
			"section.link_help": "Link this empty schema to a Jira schema. Nothing is imported yet: the next pull for this Source fills it and keeps this name and key prefix.",
			"section.link_jira_placeholder": "Select a Jira schema…",
			"section.link_load_failed": "Couldn't load the Jira schemas: {{message}}.",
			"section.link_button": "Link to Jira schema"
		},
		de: {
			"task.source": "Quelle",
			"task.source_placeholder": "Jira-Quelle auswählen…",
			"task.service_account": "Service-Account",
			"task.service_account_placeholder": "Service-Account auswählen…",
			"task.schema": "Schema",
			"task.schema_placeholder": "Schema auswählen…",
			"task.target": "Ziel",
			"task.target_placeholder": "Ziel auswählen…",
			"task.jira_schema": "Jira-Schema",
			"task.jira_schema_all": "Alle verknüpften Jira-Schemas in dieser Quelle",
			"task.connection_missing": "Diese Quelle hat keine Jira-Plugin-Verbindung. Richte sie unter Quellen ein, bevor du einen Pull startest.",
			"task.connection_check_failed": "Die Jira-Plugin-Verbindung konnte nicht geprüft werden. Der Pull bleibt bis zur erfolgreichen Prüfung gesperrt.",
			"task.core_target_linked": "Core-Ziel — verknüpft: {{schema}}",
			"task.core_target_new": "Core-Ziel — ein neues Schema wird angelegt.",
			"task.core_targets_all": "Core-Ziele — verknüpft: {{schemas}}",
			"task.core_target_none": "Kein verknüpftes Core-Schema. Wähle ein Jira-Schema, um ein neues Core-Schema anzulegen.",
			"task.jira_schema_help": "Wähle ein Schema, um nur dieses zu pullen.",
			"task.jira_schema_loading": "Jira-Schemas werden geladen…",
			"task.jira_schema_load_failed": "Jira-Schemas konnten nicht geladen werden: {{message}}. Der Pull kann trotzdem für alle Schemas laufen.",
			"task.include_comments": "Kommentare und Anhänge mitnehmen",
			"task.include_comments_help": "Importiert auch Jira-Kommentare und -Anhänge. Der Pull dauert länger, weil jedes Objekt zusätzliche Jira-Anfragen braucht.",
			"task.destination": "Ziel-Jira-Schema",
			"task.destination_placeholder": "Ziel auswählen…",
			"task.destination_load_failed": "Jira-Schemas für dieses Ziel konnten nicht geladen werden: {{message}}. Prüfe den Service-Account und wähle das Ziel erneut.",
			"task.precheck_checking": "Es wird geprüft, ob das Ziel leer ist…",
			"task.precheck_empty": "Das Ziel ist leer. Bereit für den Export.",
			"task.precheck_not_empty_one": "Das Ziel enthält bereits {{count}} Objekttyp. Ein Export braucht ein leeres Jira-Schema – wähle ein leeres oder nutze den Push.",
			"task.precheck_not_empty_other": "Das Ziel enthält bereits {{count}} Objekttypen. Ein Export braucht ein leeres Jira-Schema – wähle ein leeres oder nutze den Push.",
			"task.precheck_failed": "Das Ziel konnte nicht geprüft werden: {{message}}. Der Export bleibt gesperrt, bis die Prüfung gelingt.",
			"task.precheck_retry": "Erneut prüfen",
			"task.no_sources": "Es gibt noch keine Jira-Quellen. Lege eine unter Quellen an.",
			"task.no_targets": "Es gibt noch keine Jira-Ziele. Lege eines unter Ziele an.",
			"task.no_service_accounts": "Diese {{kind}} hat noch keinen verwendbaren Service-Account. Lege einen unter Service-Accounts an.",
			"task.action_open_sources": "Quellen öffnen",
			"task.action_open_targets": "Ziele öffnen",
			"task.action_open_service_accounts": "Service-Accounts öffnen",
			"connector.strategy_label": "Standard-Konfliktbehandlung",
			"connector.strategy_manual": "Manuell prüfen",
			"connector.strategy_prefer_local": "IndraMesh bevorzugen",
			"connector.strategy_prefer_remote": "Jira bevorzugen",
			"connector.role_help_source": "Gilt beim Pull aus Jira.",
			"connector.role_help_target": "Gilt beim Push nach Jira.",
			"connector.scope_help": "Gilt für ab jetzt erkannte Konflikte. Bereits offene Konflikte bleiben unverändert.",
			"connector.stale_state": "Die Adresse wurde nach dem Binden geändert; die Jira-Synchronisation ist für diesen Datensatz gesperrt. Neu-Binden ist noch nicht verfügbar.",
			"connector.url_changed_warning": "Du hast die Adresse geändert. Beim Speichern wird die Jira-Synchronisation für diesen Datensatz gesperrt, weil Neu-Binden noch nicht verfügbar ist.",
			"connector.load_failed": "Die Jira-Verbindungseinstellungen konnten nicht geladen werden. Öffne den Dialog erneut, um es noch einmal zu versuchen.",
			"connector.bind_invalid_url": "Für {{kind}} ist eine gültige http(s)-Adresse nötig, bevor die Jira-Verbindung gespeichert werden kann.",
			"connector.bind_not_jira": "Dies ist kein Jira-{{kind}} mehr, daher kann keine Jira-Verbindung gespeichert werden.",
			"connector.bind_failed": "Die Jira-Verbindung konnte nicht gespeichert werden. Prüfe die Adresse und versuche es erneut.",
			"connector.account_help": "Wird mit den Connector-Einstellungen gespeichert und für den Verbindungstest und Jira-Tasks vorausgewählt.",
			"connector.test_button": "Verbindung testen",
			"connector.tested_url_hint": "Testet die gespeicherte Adresse {{url}}.",
			"connector.unsaved_url_hint": "Speichere zuerst, um die eben eingegebene Adresse zu testen.",
			"connector.new_record_test_disabled": "Speichere diese(n) {{kind}} zuerst. Danach ist der Verbindungstest verfügbar: Ordne der/dem {{kind}} einen Service-Account zu und teste hier.",
			"connector.no_account_test_disabled": "Lege einen Service-Account für diese(n) {{kind}} an, um die Verbindung zu testen.",
			"connector.manage_link": "Service-Accounts verwalten",
			"connector.result_ok": "Verbunden. Jira hat den Service-Account akzeptiert.",
			"connector.result_auth": "Jira hat die Zugangsdaten abgelehnt. Aktualisiere das Token unter Service-Accounts und teste erneut.",
			"connector.result_permission": "Die Anmeldung klappt, aber der Account darf keine Assets-Schemas lesen. Gib ihm in Jira Assets-Zugriff und teste erneut.",
			"connector.result_network": "Jira antwortet unter {{url}} nicht. Prüfe die Adresse und ob Jira von IndraMesh aus erreichbar ist.",
			"connector.result_not_found": "In Jira gibt es kein Schema mit dieser ID. Prüfe das Schema und teste erneut.",
			"connector.result_error": "Der Verbindungstest ist fehlgeschlagen. Prüfe Adresse und Service-Account und teste erneut.",
			"section.schema.title": "Jira-Sync",
			"section.heading_link_direction": "Verknüpfung und Richtung",
			"section.heading_status": "Status",
			"section.heading_attributes": "Attribute",
			"section.schema_id_label": "Jira-Schema",
			"section.object_type_id_label": "Jira-ObjectType-ID",
			"section.object_label": "Jira-Objekt",
			"section.object_id_hint": "ID {{id}}",
			"section.object_id_label": "Jira-Objekt-ID",
			"section.sync_label": "Synchronisation",
			"section.attribute_jira_id": "Jira-ID",
			"section.attribute_name": "Attribut",
			"section.sync_direction_column": "Sync-Richtung",
			"section.effective_direction_column": "Effektive Richtung",
			"section.not_in_jira": "Nicht in Jira",
			"section.unlinked_schema": "Dieses Schema ist nicht mit Jira verknüpft. Starte einen Jira-Pull für seine Source, um es zu verknüpfen.",
			"section.unlinked_object_type": "Dieser ObjectType ist nicht mit Jira verknüpft.",
			"section.unlinked_object": "Dieses Objekt ist nicht mit Jira verknüpft.",
			"section.stale": "Veraltet: Die Source zeigt jetzt auf eine andere Jira-Adresse. Starte einen Pull, um dieses Schema neu zu verknüpfen.",
			"section.last_pull": "Letzter Pull",
			"section.last_push": "Letzter Push",
			"section.run_completed": "Abgeschlossen {{date}}",
			"section.run_failed": "Fehlgeschlagen {{date}}",
			"section.run_cancelled": "Abgebrochen {{date}}",
			"section.run_running": "Läuft seit {{date}}",
			"section.run_queued": "In Warteschlange",
			"section.never_pulled": "Noch nie gepullt",
			"section.never_pushed": "Noch nie gepusht",
			"section.unfinished_pulls": "Unvollendete Pulls",
			"section.unfinished_none": "Keine",
			"section.unfinished_one": "{{count}} ObjectType wird beim nächsten Pull fortgesetzt",
			"section.unfinished_other": "{{count}} ObjectTypes werden beim nächsten Pull fortgesetzt",
			"section.open_conflicts": "Offene Konflikte",
			"section.conflicts_none": "Keine",
			"section.conflicts_summary": "{{pull}} aus Pulls, {{push}} aus Pushes",
			"section.review_conflicts": "Konflikte prüfen",
			"section.loading": "Jira-Details werden geladen…",
			"section.load_error": "Die Jira-Details konnten nicht geladen werden: {{message}}.",
			"section.retry": "Erneut versuchen",
			"section.object_syncing": "Mit Jira synchronisiert",
			"section.object_detached": "Von Jira gelöst",
			"section.object_lock_hint": "Werte von Nur-Pull-Attributen sind hier gesperrt und ändern sich nur durch einen Jira-Pull. Löse das Objekt, um sie zu bearbeiten.",
			"section.detach_button": "Von Jira lösen",
			"section.reattach_button": "Wieder an Jira anbinden",
			"section.detach_confirm": "„{{name}}“ von Jira lösen? Die Werte bleiben erhalten und sind hier bearbeitbar. Pulls und Pushes überspringen dieses Objekt, bis du es wieder anbindest.",
			"section.reattach_confirm": "„{{name}}“ wieder an Jira anbinden? Nur-Pull-Werte werden wieder gesperrt und beim nächsten Pull durch die Werte aus Jira ersetzt.",
			"section.detach_toast": "„{{name}}“ von Jira gelöst.",
			"section.reattach_toast": "„{{name}}“ wieder an Jira angebunden.",
			"section.detach_error": "„{{name}}“ konnte nicht von Jira gelöst werden: {{message}}.",
			"section.reattach_error": "„{{name}}“ konnte nicht wieder an Jira angebunden werden: {{message}}.",
			"section.detach_stale": "Die Jira-Verknüpfung dieses Objekts ist veraltet. Starte einen Pull für seine Source und versuche es erneut.",
			"direction.label": "Sync-Richtung",
			"direction.inherit": "Erben",
			"direction.default_both": "Standard (beide Richtungen)",
			"direction.both": "Beide Richtungen",
			"direction.pull": "Nur aus Jira übernehmen",
			"direction.push": "Nur nach Jira übertragen",
			"direction.none": "Nicht synchronisieren",
			"direction.effective": "Effektiv: {{direction}}.",
			"direction.origin_attribute": "Hier festgelegt.",
			"direction.origin_object_type": "Vom ObjectType geerbt.",
			"direction.origin_schema": "Vom Schema geerbt.",
			"direction.origin_default": "Standard, weil keine Ebene eine Richtung festlegt.",
			"direction.lock_hint": "In IndraMesh gesperrt: Werte ändern sich nur durch einen Jira-Pull.",
			"direction.saving": "Wird gespeichert…",
			"direction.saved": "Gespeichert",
			"direction.save_error": "Die Richtung konnte nicht gespeichert werden: {{message}}. Die vorherige Einstellung gilt weiter.",
			"link.open_in_jira": "{{id}} in Jira öffnen (öffnet in neuem Tab)",
			"badge.media.label": "Jira",
			"badge.media.title": "Aus Jira importiert. Hier schreibgeschützt.",
			"badge.object.linked.label": "Jira",
			"badge.object.detached.label": "Gelöst",
			"badge.attribute.pull_only.label": "Nur Pull",
			"badge.attribute.pull_only.title": "Ändert sich nur durch einen Jira-Pull. Löse das Objekt, um den Wert hier zu bearbeiten.",
			"conflicts.nav.label": "Jira-Konflikte",
			"conflicts.nav.badge_one": "{{count}} offener Jira-Konflikt",
			"conflicts.nav.badge_other": "{{count}} offene Jira-Konflikte",
			"conflicts.nav.badge_overflow": "Mehr als 99 offene Jira-Konflikte",
			"conflicts.page.title": "Jira-Konflikte",
			"conflicts.page.description": "Werte, die sich seit dem letzten Sync sowohl in IndraMesh als auch in Jira geändert haben. Vergleich beide Seiten und wähle dann, welche Werte erhalten bleiben.",
			"conflicts.page.empty_title": "Keine offenen Jira-Konflikte",
			"conflicts.page.empty_body": "Ändert sich ein Wert zwischen zwei Syncs sowohl in IndraMesh als auch in Jira, erscheint der Konflikt hier zur Entscheidung.",
			"conflicts.page.no_results_title": "Keine Konflikte passen zu diesen Filtern",
			"conflicts.page.no_results_body": "Ändere den Schema-, Richtungs- oder Statusfilter, um andere Konflikte zu sehen.",
			"conflicts.page.load_error_title": "Jira-Konflikte konnten nicht geladen werden",
			"conflicts.page.load_error": "Jira-Konflikte konnten nicht geladen werden: {{message}}. Lade die Seite neu, um es erneut zu versuchen.",
			"conflicts.page.no_access_title": "Du hast keinen Zugriff auf Jira-Konflikte",
			"conflicts.page.no_access": "Bitte einen Administrator um die Leseberechtigung für Konflikte.",
			"conflicts.filter.schema": "Schema",
			"conflicts.filter.schema_all": "Alle Schemas",
			"conflicts.filter.direction": "Erkannt bei",
			"conflicts.filter.direction_all": "Pulls und Pushes",
			"conflicts.filter.direction_pull": "Pulls",
			"conflicts.filter.direction_push": "Pushes",
			"conflicts.filter.status": "Status",
			"conflicts.column.object": "Objekt",
			"conflicts.column.attribute": "Attribut",
			"conflicts.column.schema": "Schema",
			"conflicts.column.direction": "Erkannt bei",
			"conflicts.column.changed": "Zuletzt geändert",
			"conflicts.column.status": "Status",
			"conflicts.column.actions": "Aktionen",
			"conflicts.direction.pull": "Pull",
			"conflicts.direction.push": "Push",
			"conflicts.status.pending": "Offen",
			"conflicts.status.resolved_keep_local": "IndraMesh-Werte behalten",
			"conflicts.status.resolved_keep_remote": "Jira-Werte behalten",
			"conflicts.status.resolved_auto": "Automatisch aufgelöst",
			"conflicts.row.review": "Prüfen",
			"conflicts.row.details": "Details",
			"conflicts.row.review_aria": "Konflikt für {{attribute}} an „{{object}}“ prüfen",
			"conflicts.row.details_aria": "Details für {{attribute}} an „{{object}}“ anzeigen",
			"conflicts.detail.summary_one": "{{count}} Wert unterscheidet sich zwischen IndraMesh und Jira.",
			"conflicts.detail.summary_other": "{{count}} Werte unterscheiden sich zwischen IndraMesh und Jira.",
			"conflicts.detail.summary_plain": "IndraMesh und Jira haben unterschiedliche Werte.",
			"conflicts.detail.left": "In IndraMesh ({{count}})",
			"conflicts.detail.right": "In Jira ({{count}})",
			"conflicts.item.not_in_jira": "Nicht in Jira",
			"conflicts.item.not_in_indramesh": "Nicht in IndraMesh",
			"conflicts.no_value": "Kein Wert",
			"conflicts.value.yes": "Ja",
			"conflicts.value.no": "Nein",
			"conflicts.reference_fallback": "Jira-Objekt {{id}}",
			"conflicts.account.label": "Service-Account",
			"conflicts.account.placeholder": "Service-Account auswählen…",
			"conflicts.account.help": "Wird verwendet, um die behaltenen Werte zu schreiben.",
			"conflicts.account.none_help": "Für diesen Konflikt gibt es noch keinen passenden Service-Account. Lege unter Service-Accounts einen an.",
			"conflicts.account.manage": "Service-Accounts verwalten",
			"conflicts.action.keep_local": "IndraMesh-Werte behalten",
			"conflicts.action.keep_remote": "Jira-Werte behalten",
			"conflicts.action.consequence": "„IndraMesh-Werte behalten“ schreibt diese Werte nach Jira. „Jira-Werte behalten“ ersetzt sie in IndraMesh.",
			"conflicts.resolved.keep_local": "IndraMesh-Werte am {{date}} behalten.",
			"conflicts.resolved.keep_remote": "Jira-Werte am {{date}} behalten.",
			"conflicts.resolved.auto": "Am {{date}} automatisch aufgelöst.",
			"conflicts.toast.keep_local": "IndraMesh-Werte für {{attribute}} an „{{object}}“ behalten.",
			"conflicts.toast.keep_remote": "Jira-Werte für {{attribute}} an „{{object}}“ behalten.",
			"conflicts.error.stale_version": "Jemand anderes hat diesen Konflikt geändert. Die Liste wird aktualisiert, prüfe ihn erneut.",
			"conflicts.error.no_link": "Dieses Objekt ist nicht mehr mit Jira verknüpft oder die Verknüpfung ist veraltet. Führe einen Pull für die Source aus und prüfe den Konflikt erneut.",
			"conflicts.error.direction": "Dieses Attribut synchronisiert nicht mehr in beide Richtungen, daher kann der Konflikt hier nicht aufgelöst werden. Stelle seine Richtung zuerst auf Beide Richtungen.",
			"conflicts.error.jira": "Jira hat die Änderung nicht akzeptiert: {{message}}. Der Konflikt bleibt offen.",
			"conflicts.error.generic": "Der Konflikt konnte nicht aufgelöst werden: {{message}}.",
			"conflicts.load_more": "Weitere Konflikte laden",
			"task.target_schema": "Core-Schema",
			"task.target_schema_help": "In ein bestehendes leeres Schema dieser Source pullen, um dessen Namen und Key-Präfix zu behalten, oder ein neues Schema mit dem Namen des Jira-Schemas anlegen.",
			"task.target_schema_new": "Neues Schema anlegen",
			"task.core_target_adopt": "Core-Ziel — das bestehende Schema {{schema}} wird verknüpft.",
			"task.target_schema_none": "Diese Source hat kein leeres Schema, in das gepullt werden kann. Um Name und Key-Präfix selbst festzulegen, lege zuerst ein leeres Schema für diese Source an.",
			"section.link_requires_empty": "Nur ein leeres Schema (ohne Objekttypen) kann hier mit einem bestehenden Jira-Schema verknüpft werden.",
			"section.link_toast": "Mit Jira verknüpft. Der nächste Pull dieser Source füllt dieses Schema.",
			"section.link_error": "Das Schema konnte nicht verknüpft werden: {{message}}.",
			"section.link_help": "Verknüpfe dieses leere Schema mit einem Jira-Schema. Es wird noch nichts importiert: Der nächste Pull dieser Source füllt es und behält Name und Key-Präfix.",
			"section.link_jira_placeholder": "Jira-Schema auswählen…",
			"section.link_load_failed": "Die Jira-Schemas konnten nicht geladen werden: {{message}}.",
			"section.link_button": "Mit Jira-Schema verknüpfen"
		}
	},
	pages: [{
		id: "jira-conflicts",
		path: "conflicts",
		component: Tt
	}],
	navItems: [{
		id: "jira-conflicts",
		label: "Jira conflicts",
		labelKey: "conflicts.nav.label",
		path: "/plugins/jira/conflicts",
		icon: "git-compare",
		group: "Integration",
		permission: "conflicts:read",
		badge: St,
		badgeLabel: Ct
	}],
	taskForms: [
		{
			id: "jira-task-form-pull",
			tasktype: "jira.sync-pull",
			component: ce
		},
		{
			id: "jira-task-form-push",
			tasktype: "jira.sync-push",
			component: le
		},
		{
			id: "jira-task-form-export",
			tasktype: "jira.sync-export",
			component: de
		}
	],
	connectorConfigEditors: [{
		id: "jira-connector-editor",
		connector_type: "jira",
		label: "Jira",
		roles: ["source", "target"],
		component: _e,
		afterCoreSave: ge
	}],
	schemaSettingsSections: [{
		id: "jira-schema-sync",
		label: "Jira sync",
		labelKey: "section.schema.title",
		permission: "sync:read",
		component: qe
	}],
	objectTypeSettingsSections: [{
		id: "jira-object-type-sync",
		label: "Jira sync",
		labelKey: "section.schema.title",
		permission: "sync:read",
		component: Ye
	}],
	objectDetailSections: [{
		id: "jira-object-sync",
		label: "Jira",
		permission: "sync:read",
		component: Xe
	}],
	itemBadges: [
		{
			id: "jira-media-badges",
			kinds: ["comment", "file"],
			resolve: At
		},
		{
			id: "jira-object-badges",
			kinds: ["object"],
			permission: "sync:read",
			resolve: Ft
		},
		{
			id: "jira-attribute-badges",
			kinds: ["attribute"],
			permission: "sync:read",
			resolve: It
		}
	]
};
export { Lt as default };
