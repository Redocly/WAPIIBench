from __future__ import annotations

from logging import Logger

import regex as re


class GenerationRule:
    """
    A generation rule defines what a generated string must look like in terms of a regular expression.
    Each rule has a starting and a stopping condition to limit the span in which the rule must be satisfied.
    :param starter: The starting condition or ``None``, if the rule should be active immediately
    :param stopper: The stopping condition or ``None``, if the rule should never be deactivated
    :param body: The regex that must be satisfied while this rule is active (must end with the stopper)
    """

    def __init__(self, starter: str | None, stopper: str | None, body: str, name: str = "") -> None:
        self.starter = starter  # None means always
        self.stopper = stopper  # None means never
        self.body = body
        self.name = name

        self._starter_compiled = re.compile(starter, re.ASCII) if starter else None
        self._stopper_compiled = re.compile(stopper, re.ASCII) if stopper else None
        self._body_compiled = re.compile(body, re.ASCII)

        self.active = starter is None
        self.position = 0

    def __repr__(self) -> str:
        starter = self.starter
        stopper = self.stopper
        body = self.body
        name = self.name
        return f"GenerationRule({name=}, {starter=}, {stopper=}, {body=})"

    def __str__(self) -> str:
        name = self.name
        starter = self.starter
        stopper = self.stopper
        body = self.body
        active = self.active
        position = self.position
        return f"GenerationRule {name}\n\t{starter=}\n\t{stopper=}\n\t{body[:100]=}\n\t{active=}\n\t{position=}"

    def reset(self) -> None:
        """
        Reset the internal state of this rule.
        """
        self.active = self.starter is None
        self.position = 0

    def update(self, completion: str) -> bool:
        """
        Check the current completion to see if this generation rule should be activated or deactivated, respectively.
        When calling update repeatedly, the new completion argument must be equal to the old one except for a suffix.
        :param completion: The new completion
        :return: Whether this rule is active after this update
        """
        while True:
            if self.active:
                # If stopper is None, stay active forever; otherwise check if the stop condition is fulfilled
                if self.stopper is None:
                    break
                match = self._stopper_compiled.search(completion, pos=self.position)
                if not match:
                    break
                self.active = False
                self.position = match.end()
            else:
                # If starter is None, the rule was active in the beginning but now stays inactive forever;
                # otherwise check if the start condition is fulfilled
                if self.starter is None:
                    break
                match = self._starter_compiled.search(completion, pos=self.position)
                if not match:
                    break
                self.active = True
                self.position = match.end()

        return self.active

    def is_valid_continuation(self, completion: str, timeout: float | None = None) -> bool:
        """
        Check if the given completion would be a valid continuation of the string generated so far.
        Must only be called while this generation rule is active.
        :param completion: The completion so far with a new suffix
        :param timeout: Cancel regex matching after this number of seconds and raise a ``TimeoutError``.
        :return: If completion satisfies this generation rule
        """
        assert self.active
        match = self._body_compiled.match(completion, pos=self.position, partial=True, timeout=timeout)
        return match is not None

    def match_whole_code(self, code: str, partial: bool = False) -> re.Match | None:
        """
        Check if this rule matches the given code.
        :param code: The code to match against
        :param partial: If a partial match is sufficient
        :return: The first found ``Match`` or ``None``
        """
        starter_match = self._starter_compiled.search(code, partial=partial)
        if not starter_match or (partial and starter_match.partial):
            return starter_match
        body_match = self._body_compiled.match(code, pos=starter_match.end(), partial=partial)
        if not body_match or (partial and body_match.partial):
            return body_match
        stopper_match = self._stopper_compiled.search(code, pos=body_match.start(), partial=partial)
        if not stopper_match or (partial and stopper_match.partial):
            return stopper_match
        return body_match

    def export(self) -> str:
        """
        Convert this rule to a regex representation that can be used in other applications.
        :return: This generation rule as a regex string
        """
        return self.starter + self.body if self.starter is not None else self.body


class GenerationRuleset(list[GenerationRule]):
    """
    A generation ruleset is a list of generation rules plus some operations on them.
    """

    def reset(self) -> None:
        """
        Reset the internal state of this ruleset.
        """
        for rule in iter(self):
            rule.reset()

    def update(self, completion: str) -> bool:
        """
        Update all generation rules in this generation ruleset.
        :param completion: The new completion
        :return: Whether any rule is active after this update
        """
        any_active = False
        for rule in iter(self):
            any_active = rule.update(completion) or any_active
        return any_active

    def is_valid_continuation(self, completion: str, timeout: float | None = None) -> bool:
        """
        Check if the given completion would be a valid continuation of the string generated so far.
        :param completion: The completion so far with a new suffix
        :param timeout: Cancel regex matching after this number of seconds and raise a ``TimeoutError``
        :return: If completion satisfies all active rules in this ruleset
        """
        for rule in iter(self):
            if rule.active and not rule.is_valid_continuation(completion, timeout=timeout):
                return False
        return True

    def match_whole_code(self, code: str, excluded: list[str] | None = None, partial: bool = False) -> re.Match | None:
        """
        Check if any rule in this ruleset matches the given code.
        :param code: The code to match against
        :param excluded: Names of rules to exclude
        :param partial: If a partial match is sufficient
        :return: The first ``Match`` or ``None``
        """
        for rule in iter(self):
            if excluded is not None and rule.name in excluded:
                continue
            match = rule.match_whole_code(code, partial=partial)
            if match:
                return match
        return None

    def has_active_rules(self) -> bool:
        """
        Check if any rule in this ruleset is currently active.
        :return: If any rule is active
        """
        for rule in iter(self):
            if rule.active:
                return True
        return False

    def get_by_name(self, name: str) -> GenerationRule | None:
        """
        Get a generation rule by its name.
        :param name: The name of the wanted rule
        :return: The rule or ``None`` if it was not found
        """
        for rule in iter(self):
            if rule.name == name:
                return rule
        return None

    def print_state(self, completion: str | None = None, active_only: bool = True, logger: Logger = None) -> None:
        """
        Print the state of all rules in this ruleset for debugging purposes.
        :param completion: The current completion for more detailed prints
        :param active_only: If only active rules should be printed
        :param logger: Optional logger to use instead of ``println``
        """
        if active_only and not self.has_active_rules():
            state = "No active rules"
            if logger is None:
                print(state)
            else:
                logger.info(state)
            return

        for rule in iter(self):
            active = rule.active
            if active_only and not active:
                continue
            name = rule.name
            position = rule.position
            state = f"GenerationRule {name}\n\t{active=}\n\t{position=}"
            if completion:
                code_snippet = completion[max(0, position - 50):min(position + 50, len(completion))]
                state += f"\t=>\t{repr(completion[position])}\n{repr(code_snippet)}\n"
            if logger is None:
                print(state)
            else:
                logger.info("\n" + state)

    def __str__(self) -> str:
        return "\n".join([str(rule) for rule in iter(self)])

    def export(self) -> str:
        """
        Convert this ruleset to a regex representation that can be used in other applications.
        :return: This generation ruleset as a regex string
        """
        from openapi_utils import join_alternatives
        rule_regex = join_alternatives(
            [rule.export() for rule in iter(self) if rule.name != "funnel"], inner_parentheses=False)
        return fr"(?:(?s:.)*?{rule_regex})+"
