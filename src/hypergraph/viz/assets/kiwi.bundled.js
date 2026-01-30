var kiwi = (() => {
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __commonJS = (cb, mod) => function __require() {
    return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
  };

  // node_modules/kiwi.js/lib/kiwi.js
  var require_kiwi = __commonJS({
    "node_modules/kiwi.js/lib/kiwi.js"(exports, module) {
      (function(global, factory) {
        typeof exports === "object" && typeof module !== "undefined" ? factory(exports) : typeof define === "function" && define.amd ? define(["exports"], factory) : (global = global || self, factory(global.kiwi = {}));
      })(exports, function(exports2) {
        "use strict";
        function createMap() {
          return new IndexedMap();
        }
        var IndexedMap = (
          /** @class */
          (function() {
            function IndexedMap2() {
              this.index = {};
              this.array = [];
            }
            IndexedMap2.prototype.size = function() {
              return this.array.length;
            };
            IndexedMap2.prototype.empty = function() {
              return this.array.length === 0;
            };
            IndexedMap2.prototype.itemAt = function(index) {
              return this.array[index];
            };
            IndexedMap2.prototype.contains = function(key) {
              return this.index[key.id()] !== void 0;
            };
            IndexedMap2.prototype.find = function(key) {
              var i = this.index[key.id()];
              return i === void 0 ? void 0 : this.array[i];
            };
            IndexedMap2.prototype.setDefault = function(key, factory) {
              var i = this.index[key.id()];
              if (i === void 0) {
                var pair = new Pair(key, factory());
                this.index[key.id()] = this.array.length;
                this.array.push(pair);
                return pair;
              } else {
                return this.array[i];
              }
            };
            IndexedMap2.prototype.insert = function(key, value) {
              var pair = new Pair(key, value);
              var i = this.index[key.id()];
              if (i === void 0) {
                this.index[key.id()] = this.array.length;
                this.array.push(pair);
              } else {
                this.array[i] = pair;
              }
              return pair;
            };
            IndexedMap2.prototype.erase = function(key) {
              var i = this.index[key.id()];
              if (i === void 0) {
                return void 0;
              }
              this.index[key.id()] = void 0;
              var pair = this.array[i];
              var last = this.array.pop();
              if (pair !== last) {
                this.array[i] = last;
                this.index[last.first.id()] = i;
              }
              return pair;
            };
            IndexedMap2.prototype.copy = function() {
              var copy = new IndexedMap2();
              for (var i = 0; i < this.array.length; i++) {
                var pair = this.array[i].copy();
                copy.array[i] = pair;
                copy.index[pair.first.id()] = i;
              }
              return copy;
            };
            return IndexedMap2;
          })()
        );
        var Pair = (
          /** @class */
          (function() {
            function Pair2(first, second) {
              this.first = first;
              this.second = second;
            }
            Pair2.prototype.copy = function() {
              return new Pair2(this.first, this.second);
            };
            return Pair2;
          })()
        );
        var Variable = (
          /** @class */
          (function() {
            function Variable2(name) {
              if (name === void 0) {
                name = "";
              }
              this._value = 0;
              this._context = null;
              this._id = VarId++;
              this._name = name;
            }
            Variable2.prototype.id = function() {
              return this._id;
            };
            Variable2.prototype.name = function() {
              return this._name;
            };
            Variable2.prototype.setName = function(name) {
              this._name = name;
            };
            Variable2.prototype.context = function() {
              return this._context;
            };
            Variable2.prototype.setContext = function(context) {
              this._context = context;
            };
            Variable2.prototype.value = function() {
              return this._value;
            };
            Variable2.prototype.setValue = function(value) {
              this._value = value;
            };
            Variable2.prototype.plus = function(value) {
              return new Expression(this, value);
            };
            Variable2.prototype.minus = function(value) {
              return new Expression(this, typeof value === "number" ? -value : [-1, value]);
            };
            Variable2.prototype.multiply = function(coefficient) {
              return new Expression([coefficient, this]);
            };
            Variable2.prototype.divide = function(coefficient) {
              return new Expression([1 / coefficient, this]);
            };
            Variable2.prototype.toJSON = function() {
              return {
                name: this._name,
                value: this._value
              };
            };
            Variable2.prototype.toString = function() {
              return this._context + "[" + this._name + ":" + this._value + "]";
            };
            return Variable2;
          })()
        );
        var VarId = 0;
        var Expression = (
          /** @class */
          (function() {
            function Expression2() {
              var parsed = parseArgs(arguments);
              this._terms = parsed.terms;
              this._constant = parsed.constant;
            }
            Expression2.prototype.terms = function() {
              return this._terms;
            };
            Expression2.prototype.constant = function() {
              return this._constant;
            };
            Expression2.prototype.value = function() {
              var result = this._constant;
              for (var i = 0, n = this._terms.size(); i < n; i++) {
                var pair = this._terms.itemAt(i);
                result += pair.first.value() * pair.second;
              }
              return result;
            };
            Expression2.prototype.plus = function(value) {
              return new Expression2(this, value);
            };
            Expression2.prototype.minus = function(value) {
              return new Expression2(this, typeof value === "number" ? -value : [-1, value]);
            };
            Expression2.prototype.multiply = function(coefficient) {
              return new Expression2([coefficient, this]);
            };
            Expression2.prototype.divide = function(coefficient) {
              return new Expression2([1 / coefficient, this]);
            };
            Expression2.prototype.isConstant = function() {
              return this._terms.size() == 0;
            };
            Expression2.prototype.toString = function() {
              var result = this._terms.array.map(function(pair) {
                return pair.second + "*" + pair.first.toString();
              }).join(" + ");
              if (!this.isConstant() && this._constant !== 0) {
                result += " + ";
              }
              result += this._constant;
              return result;
            };
            return Expression2;
          })()
        );
        function parseArgs(args) {
          var constant = 0;
          var factory = function() {
            return 0;
          };
          var terms = createMap();
          for (var i = 0, n = args.length; i < n; ++i) {
            var item = args[i];
            if (typeof item === "number") {
              constant += item;
            } else if (item instanceof Variable) {
              terms.setDefault(item, factory).second += 1;
            } else if (item instanceof Expression) {
              constant += item.constant();
              var terms2 = item.terms();
              for (var j = 0, k = terms2.size(); j < k; j++) {
                var termPair = terms2.itemAt(j);
                terms.setDefault(termPair.first, factory).second += termPair.second;
              }
            } else if (item instanceof Array) {
              if (item.length !== 2) {
                throw new Error("array must have length 2");
              }
              var value = item[0];
              var value2 = item[1];
              if (typeof value !== "number") {
                throw new Error("array item 0 must be a number");
              }
              if (value2 instanceof Variable) {
                terms.setDefault(value2, factory).second += value;
              } else if (value2 instanceof Expression) {
                constant += value2.constant() * value;
                var terms2 = value2.terms();
                for (var j = 0, k = terms2.size(); j < k; j++) {
                  var termPair = terms2.itemAt(j);
                  terms.setDefault(termPair.first, factory).second += termPair.second * value;
                }
              } else {
                throw new Error("array item 1 must be a variable or expression");
              }
            } else {
              throw new Error("invalid Expression argument: " + item);
            }
          }
          return { terms, constant };
        }
        var Strength = (
          /** @class */
          (function() {
            function Strength2() {
            }
            Strength2.create = function(a, b, c, w) {
              if (w === void 0) {
                w = 1;
              }
              var result = 0;
              result += Math.max(0, Math.min(1e3, a * w)) * 1e6;
              result += Math.max(0, Math.min(1e3, b * w)) * 1e3;
              result += Math.max(0, Math.min(1e3, c * w));
              return result;
            };
            Strength2.clip = function(value) {
              return Math.max(0, Math.min(Strength2.required, value));
            };
            Strength2.required = Strength2.create(1e3, 1e3, 1e3);
            Strength2.strong = Strength2.create(1, 0, 0);
            Strength2.medium = Strength2.create(0, 1, 0);
            Strength2.weak = Strength2.create(0, 0, 1);
            return Strength2;
          })()
        );
        (function(Operator) {
          Operator[Operator["Le"] = 0] = "Le";
          Operator[Operator["Ge"] = 1] = "Ge";
          Operator[Operator["Eq"] = 2] = "Eq";
        })(exports2.Operator || (exports2.Operator = {}));
        var Constraint = (
          /** @class */
          (function() {
            function Constraint2(expression, operator, rhs, strength) {
              if (strength === void 0) {
                strength = Strength.required;
              }
              this._id = CnId++;
              this._operator = operator;
              this._strength = Strength.clip(strength);
              if (rhs === void 0 && expression instanceof Expression) {
                this._expression = expression;
              } else {
                this._expression = expression.minus(rhs);
              }
            }
            Constraint2.prototype.id = function() {
              return this._id;
            };
            Constraint2.prototype.expression = function() {
              return this._expression;
            };
            Constraint2.prototype.op = function() {
              return this._operator;
            };
            Constraint2.prototype.strength = function() {
              return this._strength;
            };
            Constraint2.prototype.toString = function() {
              return this._expression.toString() + " " + ["<=", ">=", "="][this._operator] + " 0 (" + this._strength.toString() + ")";
            };
            return Constraint2;
          })()
        );
        var CnId = 0;
        var Solver = (
          /** @class */
          (function() {
            function Solver2() {
              this._cnMap = createCnMap();
              this._rowMap = createRowMap();
              this._varMap = createVarMap();
              this._editMap = createEditMap();
              this._infeasibleRows = [];
              this._objective = new Row();
              this._artificial = null;
              this._idTick = 0;
            }
            Solver2.prototype.createConstraint = function(lhs, operator, rhs, strength) {
              if (strength === void 0) {
                strength = Strength.required;
              }
              var cn = new Constraint(lhs, operator, rhs, strength);
              this.addConstraint(cn);
              return cn;
            };
            Solver2.prototype.addConstraint = function(constraint) {
              var cnPair = this._cnMap.find(constraint);
              if (cnPair !== void 0) {
                throw new Error("duplicate constraint");
              }
              var data = this._createRow(constraint);
              var row = data.row;
              var tag = data.tag;
              var subject = this._chooseSubject(row, tag);
              if (subject.type() === SymbolType.Invalid && row.allDummies()) {
                if (!nearZero(row.constant())) {
                  throw new Error("unsatisfiable constraint");
                } else {
                  subject = tag.marker;
                }
              }
              if (subject.type() === SymbolType.Invalid) {
                if (!this._addWithArtificialVariable(row)) {
                  throw new Error("unsatisfiable constraint");
                }
              } else {
                row.solveFor(subject);
                this._substitute(subject, row);
                this._rowMap.insert(subject, row);
              }
              this._cnMap.insert(constraint, tag);
              this._optimize(this._objective);
            };
            Solver2.prototype.removeConstraint = function(constraint) {
              var cnPair = this._cnMap.erase(constraint);
              if (cnPair === void 0) {
                throw new Error("unknown constraint");
              }
              this._removeConstraintEffects(constraint, cnPair.second);
              var marker = cnPair.second.marker;
              var rowPair = this._rowMap.erase(marker);
              if (rowPair === void 0) {
                var leaving = this._getMarkerLeavingSymbol(marker);
                if (leaving.type() === SymbolType.Invalid) {
                  throw new Error("failed to find leaving row");
                }
                rowPair = this._rowMap.erase(leaving);
                rowPair.second.solveForEx(leaving, marker);
                this._substitute(marker, rowPair.second);
              }
              this._optimize(this._objective);
            };
            Solver2.prototype.hasConstraint = function(constraint) {
              return this._cnMap.contains(constraint);
            };
            Solver2.prototype.addEditVariable = function(variable, strength) {
              var editPair = this._editMap.find(variable);
              if (editPair !== void 0) {
                throw new Error("duplicate edit variable");
              }
              strength = Strength.clip(strength);
              if (strength === Strength.required) {
                throw new Error("bad required strength");
              }
              var expr = new Expression(variable);
              var cn = new Constraint(expr, exports2.Operator.Eq, void 0, strength);
              this.addConstraint(cn);
              var tag = this._cnMap.find(cn).second;
              var info = { tag, constraint: cn, constant: 0 };
              this._editMap.insert(variable, info);
            };
            Solver2.prototype.removeEditVariable = function(variable) {
              var editPair = this._editMap.erase(variable);
              if (editPair === void 0) {
                throw new Error("unknown edit variable");
              }
              this.removeConstraint(editPair.second.constraint);
            };
            Solver2.prototype.hasEditVariable = function(variable) {
              return this._editMap.contains(variable);
            };
            Solver2.prototype.suggestValue = function(variable, value) {
              var editPair = this._editMap.find(variable);
              if (editPair === void 0) {
                throw new Error("unknown edit variable");
              }
              var rows = this._rowMap;
              var info = editPair.second;
              var delta = value - info.constant;
              info.constant = value;
              var marker = info.tag.marker;
              var rowPair = rows.find(marker);
              if (rowPair !== void 0) {
                if (rowPair.second.add(-delta) < 0) {
                  this._infeasibleRows.push(marker);
                }
                this._dualOptimize();
                return;
              }
              var other = info.tag.other;
              rowPair = rows.find(other);
              if (rowPair !== void 0) {
                if (rowPair.second.add(delta) < 0) {
                  this._infeasibleRows.push(other);
                }
                this._dualOptimize();
                return;
              }
              for (var i = 0, n = rows.size(); i < n; ++i) {
                var rowPair_1 = rows.itemAt(i);
                var row = rowPair_1.second;
                var coeff = row.coefficientFor(marker);
                if (coeff !== 0 && row.add(delta * coeff) < 0 && rowPair_1.first.type() !== SymbolType.External) {
                  this._infeasibleRows.push(rowPair_1.first);
                }
              }
              this._dualOptimize();
            };
            Solver2.prototype.updateVariables = function() {
              var vars = this._varMap;
              var rows = this._rowMap;
              for (var i = 0, n = vars.size(); i < n; ++i) {
                var pair = vars.itemAt(i);
                var rowPair = rows.find(pair.second);
                if (rowPair !== void 0) {
                  pair.first.setValue(rowPair.second.constant());
                } else {
                  pair.first.setValue(0);
                }
              }
            };
            Solver2.prototype._getVarSymbol = function(variable) {
              var _this = this;
              var factory = function() {
                return _this._makeSymbol(SymbolType.External);
              };
              return this._varMap.setDefault(variable, factory).second;
            };
            Solver2.prototype._createRow = function(constraint) {
              var expr = constraint.expression();
              var row = new Row(expr.constant());
              var terms = expr.terms();
              for (var i = 0, n = terms.size(); i < n; ++i) {
                var termPair = terms.itemAt(i);
                if (!nearZero(termPair.second)) {
                  var symbol = this._getVarSymbol(termPair.first);
                  var basicPair = this._rowMap.find(symbol);
                  if (basicPair !== void 0) {
                    row.insertRow(basicPair.second, termPair.second);
                  } else {
                    row.insertSymbol(symbol, termPair.second);
                  }
                }
              }
              var objective = this._objective;
              var strength = constraint.strength();
              var tag = { marker: INVALID_SYMBOL, other: INVALID_SYMBOL };
              switch (constraint.op()) {
                case exports2.Operator.Le:
                case exports2.Operator.Ge: {
                  var coeff = constraint.op() === exports2.Operator.Le ? 1 : -1;
                  var slack = this._makeSymbol(SymbolType.Slack);
                  tag.marker = slack;
                  row.insertSymbol(slack, coeff);
                  if (strength < Strength.required) {
                    var error = this._makeSymbol(SymbolType.Error);
                    tag.other = error;
                    row.insertSymbol(error, -coeff);
                    objective.insertSymbol(error, strength);
                  }
                  break;
                }
                case exports2.Operator.Eq: {
                  if (strength < Strength.required) {
                    var errplus = this._makeSymbol(SymbolType.Error);
                    var errminus = this._makeSymbol(SymbolType.Error);
                    tag.marker = errplus;
                    tag.other = errminus;
                    row.insertSymbol(errplus, -1);
                    row.insertSymbol(errminus, 1);
                    objective.insertSymbol(errplus, strength);
                    objective.insertSymbol(errminus, strength);
                  } else {
                    var dummy = this._makeSymbol(SymbolType.Dummy);
                    tag.marker = dummy;
                    row.insertSymbol(dummy);
                  }
                  break;
                }
              }
              if (row.constant() < 0) {
                row.reverseSign();
              }
              return { row, tag };
            };
            Solver2.prototype._chooseSubject = function(row, tag) {
              var cells = row.cells();
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                if (pair.first.type() === SymbolType.External) {
                  return pair.first;
                }
              }
              var type = tag.marker.type();
              if (type === SymbolType.Slack || type === SymbolType.Error) {
                if (row.coefficientFor(tag.marker) < 0) {
                  return tag.marker;
                }
              }
              type = tag.other.type();
              if (type === SymbolType.Slack || type === SymbolType.Error) {
                if (row.coefficientFor(tag.other) < 0) {
                  return tag.other;
                }
              }
              return INVALID_SYMBOL;
            };
            Solver2.prototype._addWithArtificialVariable = function(row) {
              var art = this._makeSymbol(SymbolType.Slack);
              this._rowMap.insert(art, row.copy());
              this._artificial = row.copy();
              this._optimize(this._artificial);
              var success = nearZero(this._artificial.constant());
              this._artificial = null;
              var pair = this._rowMap.erase(art);
              if (pair !== void 0) {
                var basicRow = pair.second;
                if (basicRow.isConstant()) {
                  return success;
                }
                var entering = this._anyPivotableSymbol(basicRow);
                if (entering.type() === SymbolType.Invalid) {
                  return false;
                }
                basicRow.solveForEx(art, entering);
                this._substitute(entering, basicRow);
                this._rowMap.insert(entering, basicRow);
              }
              var rows = this._rowMap;
              for (var i = 0, n = rows.size(); i < n; ++i) {
                rows.itemAt(i).second.removeSymbol(art);
              }
              this._objective.removeSymbol(art);
              return success;
            };
            Solver2.prototype._substitute = function(symbol, row) {
              var rows = this._rowMap;
              for (var i = 0, n = rows.size(); i < n; ++i) {
                var pair = rows.itemAt(i);
                pair.second.substitute(symbol, row);
                if (pair.second.constant() < 0 && pair.first.type() !== SymbolType.External) {
                  this._infeasibleRows.push(pair.first);
                }
              }
              this._objective.substitute(symbol, row);
              if (this._artificial) {
                this._artificial.substitute(symbol, row);
              }
            };
            Solver2.prototype._optimize = function(objective) {
              while (true) {
                var entering = this._getEnteringSymbol(objective);
                if (entering.type() === SymbolType.Invalid) {
                  return;
                }
                var leaving = this._getLeavingSymbol(entering);
                if (leaving.type() === SymbolType.Invalid) {
                  throw new Error("the objective is unbounded");
                }
                var row = this._rowMap.erase(leaving).second;
                row.solveForEx(leaving, entering);
                this._substitute(entering, row);
                this._rowMap.insert(entering, row);
              }
            };
            Solver2.prototype._dualOptimize = function() {
              var rows = this._rowMap;
              var infeasible = this._infeasibleRows;
              while (infeasible.length !== 0) {
                var leaving = infeasible.pop();
                var pair = rows.find(leaving);
                if (pair !== void 0 && pair.second.constant() < 0) {
                  var entering = this._getDualEnteringSymbol(pair.second);
                  if (entering.type() === SymbolType.Invalid) {
                    throw new Error("dual optimize failed");
                  }
                  var row = pair.second;
                  rows.erase(leaving);
                  row.solveForEx(leaving, entering);
                  this._substitute(entering, row);
                  rows.insert(entering, row);
                }
              }
            };
            Solver2.prototype._getEnteringSymbol = function(objective) {
              var cells = objective.cells();
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                var symbol = pair.first;
                if (pair.second < 0 && symbol.type() !== SymbolType.Dummy) {
                  return symbol;
                }
              }
              return INVALID_SYMBOL;
            };
            Solver2.prototype._getDualEnteringSymbol = function(row) {
              var ratio = Number.MAX_VALUE;
              var entering = INVALID_SYMBOL;
              var cells = row.cells();
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                var symbol = pair.first;
                var c = pair.second;
                if (c > 0 && symbol.type() !== SymbolType.Dummy) {
                  var coeff = this._objective.coefficientFor(symbol);
                  var r = coeff / c;
                  if (r < ratio) {
                    ratio = r;
                    entering = symbol;
                  }
                }
              }
              return entering;
            };
            Solver2.prototype._getLeavingSymbol = function(entering) {
              var ratio = Number.MAX_VALUE;
              var found = INVALID_SYMBOL;
              var rows = this._rowMap;
              for (var i = 0, n = rows.size(); i < n; ++i) {
                var pair = rows.itemAt(i);
                var symbol = pair.first;
                if (symbol.type() !== SymbolType.External) {
                  var row = pair.second;
                  var temp = row.coefficientFor(entering);
                  if (temp < 0) {
                    var temp_ratio = -row.constant() / temp;
                    if (temp_ratio < ratio) {
                      ratio = temp_ratio;
                      found = symbol;
                    }
                  }
                }
              }
              return found;
            };
            Solver2.prototype._getMarkerLeavingSymbol = function(marker) {
              var dmax = Number.MAX_VALUE;
              var r1 = dmax;
              var r2 = dmax;
              var invalid = INVALID_SYMBOL;
              var first = invalid;
              var second = invalid;
              var third = invalid;
              var rows = this._rowMap;
              for (var i = 0, n = rows.size(); i < n; ++i) {
                var pair = rows.itemAt(i);
                var row = pair.second;
                var c = row.coefficientFor(marker);
                if (c === 0) {
                  continue;
                }
                var symbol = pair.first;
                if (symbol.type() === SymbolType.External) {
                  third = symbol;
                } else if (c < 0) {
                  var r = -row.constant() / c;
                  if (r < r1) {
                    r1 = r;
                    first = symbol;
                  }
                } else {
                  var r = row.constant() / c;
                  if (r < r2) {
                    r2 = r;
                    second = symbol;
                  }
                }
              }
              if (first !== invalid) {
                return first;
              }
              if (second !== invalid) {
                return second;
              }
              return third;
            };
            Solver2.prototype._removeConstraintEffects = function(cn, tag) {
              if (tag.marker.type() === SymbolType.Error) {
                this._removeMarkerEffects(tag.marker, cn.strength());
              }
              if (tag.other.type() === SymbolType.Error) {
                this._removeMarkerEffects(tag.other, cn.strength());
              }
            };
            Solver2.prototype._removeMarkerEffects = function(marker, strength) {
              var pair = this._rowMap.find(marker);
              if (pair !== void 0) {
                this._objective.insertRow(pair.second, -strength);
              } else {
                this._objective.insertSymbol(marker, -strength);
              }
            };
            Solver2.prototype._anyPivotableSymbol = function(row) {
              var cells = row.cells();
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                var type = pair.first.type();
                if (type === SymbolType.Slack || type === SymbolType.Error) {
                  return pair.first;
                }
              }
              return INVALID_SYMBOL;
            };
            Solver2.prototype._makeSymbol = function(type) {
              return new Symbol2(type, this._idTick++);
            };
            return Solver2;
          })()
        );
        function nearZero(value) {
          var eps = 1e-8;
          return value < 0 ? -value < eps : value < eps;
        }
        function createCnMap() {
          return createMap();
        }
        function createRowMap() {
          return createMap();
        }
        function createVarMap() {
          return createMap();
        }
        function createEditMap() {
          return createMap();
        }
        var SymbolType;
        (function(SymbolType2) {
          SymbolType2[SymbolType2["Invalid"] = 0] = "Invalid";
          SymbolType2[SymbolType2["External"] = 1] = "External";
          SymbolType2[SymbolType2["Slack"] = 2] = "Slack";
          SymbolType2[SymbolType2["Error"] = 3] = "Error";
          SymbolType2[SymbolType2["Dummy"] = 4] = "Dummy";
        })(SymbolType || (SymbolType = {}));
        var Symbol2 = (
          /** @class */
          (function() {
            function Symbol3(type, id) {
              this._id = id;
              this._type = type;
            }
            Symbol3.prototype.id = function() {
              return this._id;
            };
            Symbol3.prototype.type = function() {
              return this._type;
            };
            return Symbol3;
          })()
        );
        var INVALID_SYMBOL = new Symbol2(SymbolType.Invalid, -1);
        var Row = (
          /** @class */
          (function() {
            function Row2(constant) {
              if (constant === void 0) {
                constant = 0;
              }
              this._cellMap = createMap();
              this._constant = constant;
            }
            Row2.prototype.cells = function() {
              return this._cellMap;
            };
            Row2.prototype.constant = function() {
              return this._constant;
            };
            Row2.prototype.isConstant = function() {
              return this._cellMap.empty();
            };
            Row2.prototype.allDummies = function() {
              var cells = this._cellMap;
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                if (pair.first.type() !== SymbolType.Dummy) {
                  return false;
                }
              }
              return true;
            };
            Row2.prototype.copy = function() {
              var theCopy = new Row2(this._constant);
              theCopy._cellMap = this._cellMap.copy();
              return theCopy;
            };
            Row2.prototype.add = function(value) {
              return this._constant += value;
            };
            Row2.prototype.insertSymbol = function(symbol, coefficient) {
              if (coefficient === void 0) {
                coefficient = 1;
              }
              var pair = this._cellMap.setDefault(symbol, function() {
                return 0;
              });
              if (nearZero(pair.second += coefficient)) {
                this._cellMap.erase(symbol);
              }
            };
            Row2.prototype.insertRow = function(other, coefficient) {
              if (coefficient === void 0) {
                coefficient = 1;
              }
              this._constant += other._constant * coefficient;
              var cells = other._cellMap;
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                this.insertSymbol(pair.first, pair.second * coefficient);
              }
            };
            Row2.prototype.removeSymbol = function(symbol) {
              this._cellMap.erase(symbol);
            };
            Row2.prototype.reverseSign = function() {
              this._constant = -this._constant;
              var cells = this._cellMap;
              for (var i = 0, n = cells.size(); i < n; ++i) {
                var pair = cells.itemAt(i);
                pair.second = -pair.second;
              }
            };
            Row2.prototype.solveFor = function(symbol) {
              var cells = this._cellMap;
              var pair = cells.erase(symbol);
              var coeff = -1 / pair.second;
              this._constant *= coeff;
              for (var i = 0, n = cells.size(); i < n; ++i) {
                cells.itemAt(i).second *= coeff;
              }
            };
            Row2.prototype.solveForEx = function(lhs, rhs) {
              this.insertSymbol(lhs, -1);
              this.solveFor(rhs);
            };
            Row2.prototype.coefficientFor = function(symbol) {
              var pair = this._cellMap.find(symbol);
              return pair !== void 0 ? pair.second : 0;
            };
            Row2.prototype.substitute = function(symbol, row) {
              var pair = this._cellMap.erase(symbol);
              if (pair !== void 0) {
                this.insertRow(row, pair.second);
              }
            };
            return Row2;
          })()
        );
        exports2.Constraint = Constraint;
        exports2.Expression = Expression;
        exports2.Solver = Solver;
        exports2.Strength = Strength;
        exports2.Variable = Variable;
        Object.defineProperty(exports2, "__esModule", { value: true });
      });
    }
  });
  return require_kiwi();
})();
