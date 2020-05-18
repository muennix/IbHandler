#!/usr/bin/env python

from ib.ext.Contract import Contract
from ib.ext.Order import Order
from ib.opt import ibConnection, message
from time import sleep, strftime, localtime
import cPickle as cpickle
from threading import *
import datetime
import sys
from threading import Timer
import logging
from ib.ext.ExecutionFilter import ExecutionFilter

TickTypeBidSize = 0
TickTypeBid = 1
TickTypeAsk = 2
TickTypeAskSize = 3
TickTypeLast = 4
TickTypeLastSize = 5
TickTypeHigh = 6
TickTypeLow = 7
TickTypeVollume = 8
TickTypeClose = 9


def makeStkOrder(shares,action,account,ordertype='MKT'):
    order = Order()
    order.m_minQty = shares
    order.m_orderType = ordertype
    order.m_totalQuantity = shares
    order.m_action = str(action).upper()
    order.m_outsideRth = True #allow order to be filled ourside regular trading hours
    order.m_account = account
    return order


def makeStkContract(symbol, cur = 'USD'):
    contract = Contract()
    contract.m_symbol = symbol.replace('.',' ').replace('_',' ').replace('-',' ')
    contract.m_secType = 'STK'
    contract.m_exchange = 'SMART'
    contract.m_currency = cur
    return contract


class OpenOrder(object):
	def __init__(self,contract,vollume,price,limitprice,action):
		self.action = action
		self.vollume = vollume
		self.contract = contract
		self.limitprice = limitprice
		self.price = price
		self.bid = None
		self.ask = None
		self.ordertype = "MKT"
		self.prev_close = None #only used for consitency checks
		self.ba_offset = 0
		self.placed = False
		self.canceled = False
		self.last_adjust = None
		self.placed_date = datetime.datetime.today()
		self.adjust_periodical = False
		self.market_data_subscribed = False

	def __repr__(self):
		return str(self.contract.m_symbol)+" "+str(self.action)+" "+str(self.vollume)+" "+str(self.price)+" "+str(self.limitprice)+" "+str(self.ordertype)+" "+str(self.bid)+" "+str(self.ask)+" "+str(self.placed)+" "+str(self.adjust_periodical)

class LogEntry(object):
	def __init__(self,timestamp = None, symbol = None, ordervollume = None, targeted_price = None, limitprice = None, unique_ID = None):
		self.timestamp = timestamp
		self.symbol = symbol
		self.ordervollume = ordervollume
		self.targeted_price = targeted_price
		self.limitprice = limitprice
		self.bid = None
		self.ask = None
		self.bidsize = None
		self.asksize = None
		self.last = None
		self.lastsize = None
		self.high = None
		self.low = None
		self.vollume = None
		self.close = None
		self.avg_fill_price = None
		self.avg_costs = None
		self.fill_date = None
		self.limt_price = None
		self.action = None
		self.valid_until = None
		self.ordertype = None
		self.ba_offset = None
		self.unique_ID = unique_ID
		self.commission = None
		self.canceled = False


	def __repr__(self):
		return self.getlogstr().replace("\t"," ")

	def getlogstr(self):
		return str(self.timestamp)+"\t"+str(self.symbol)+"\t"+str(self.ordervollume)+"\t"+str(self.action)+"\t"+str(self.ordertype)+"\t"+str(self.targeted_price)+"\t"+str(self.limitprice)+"\t"+str(self.bid)+"\t"+str(self.ask)+"\t"+str(self.ba_offset)+"\t"+str(self.bidsize)+"\t"+str(self.asksize)+"\t"+str(self.last)+"\t"+str(self.lastsize)+"\t"+str(self.high)+"\t"+str(self.low)+"\t"+str(self.vollume)+"\t"+str(self.close)+"\t"+str(self.avg_fill_price)+"\t"+str(self.fill_date)+"\t"+str(self.limt_price)+"\t"+str(self.valid_until)+"\t"+str(self.unique_ID)+"\t"+str(self.commission)


def PrintProgress(Size, totalSize):
    percent = int(Size*100/totalSize)
    sys.stdout.write("%3d%%" % percent)
    sys.stdout.write("\b\b\b\b")
    sys.stdout.flush()

current_milli_time = lambda: int(round(time.time() * 1000))

class mxIBhandler(object):
	def __init__(self,account='',limit_adjust_interval = 15, max_adjust_time=5,loglevel = logging.INFO):

		self.logger = logging.getLogger('mxIBhandler')
		self.logger.setLevel(loglevel)
		#logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

		self.con = ibConnection(port=7496)
		self.NextOrderID = 100
		self.__connected = True

		self.available_cash = dict()
		self.net_liquidation = dict()
		self.buyingpower = dict()
		self.__updatePortfolioEvent = Event()
		self.__updatePortfolioEvent.clear()

		self.__OrdersFinished = Event()
		self.__OrdersFinished.set()
		self.__portfolio = dict()
		self.openorders = dict()
		self.log = dict()
		self.__SyncStockGetOrderID = None
		self.__SyncStockGetEvent = Event()
		self.__SyncStockGetEvent.set()
		self.__SyncStockGetPrice = None
		self.__SyncStockGetType = TickTypeBid 

		self.__SyncExecDetailsReqID = None
		self.__SyncExecDetailsEvent = Event()
		self.__SyncExecDetailsEvent.set()

		self.__MapExecIdtoOrderId = dict()

		self.__SyncStockGetMultipleEvent = Event()
		self.__SyncStockGetMultipleEvent.set()
		self.__SyncStockGetMultipleType = TickTypeBid 
		self.__SyncStockGetMultiplePrice = dict()
		self.__SyncStockGetMultipleIDs = dict()
		self._account = account

		self.__MapToOriginalOrderID = dict() #map orderid to unique id
		self.__MapToExecuteOrderID = dict()

		self.limit_adjust_interval = limit_adjust_interval
		self.adjist_limits_thread = None
		self.max_adjust_time = datetime.timedelta(minutes=max_adjust_time)

		#register callback handlers
		self.con.register(self.updateAccountValueHandler, 'UpdateAccountValue')
		self.con.register(self.updatePortfolioHandler, 'UpdatePortfolio')
		self.con.register(self.accountDownloadEndHandler, 'AccountDownloadEnd')
		self.con.register(self.tickPriceHandler, 'TickPrice')
		self.con.register(self.tickSizeHandler, 'TickSize')
		self.con.register(self.nextValidIdHandler, 'NextValidId')
		self.con.register(self.orderStatusHandler, 'OrderStatus')
		self.con.register(self.errorHandler, 'Error')

		self.con.register(self.execDetailsHandler, 'ExecDetails')
		self.con.register(self.execDetailsEndHandler, 'ExecDetailsEnd')
		self.con.register(self.commissionReportHandler, 'CommissionReport')

		self.logger.debug("mxIBhandler connecting...")
		self.con.connect()

		self.con.reqAccountUpdates(True, self._account)

		if not (self.__updatePortfolioEvent.wait(timeout=10) and (self.__connected == True)):
			self.logger.error("mxIBhandler connection timeout")
			raise IOError("Connection timeout")
		else:
			self.logger.debug("mxIBhandler connection succesful")


	def __enter__(self):
		return self

	def __getattr__(self, item):
		if item == "portfolio":
			self.__updatePortfolioEvent.wait()
			return self.__portfolio

	def wait_for_orders(self,timeout=None,sleeptime=1):
		self.logger.info("Waiting for orders...")
		if self.__OrdersFinished.wait(timeout) == False:
			self.logger.warning("mxIBhandler wait_for_orders timeout")
			if self.adjist_limits_thread is not None:
				self.adjist_limits_thread.cancel()
		else:
			if self.adjist_limits_thread is not None:
				self.adjist_limits_thread.cancel()
			sleep(sleeptime) #wait for portfolio update messages to be handled
			self.logger.info("All orders complete")

	def get_available_cash(self, currency = "USD"):
		#self.__updatePortfolioEvent.wait()
		sleep(5)
		if currency in self.available_cash:
			if currency in self.buyingpower and self.buyingpower[currency] < self.available_cash[currency]:
				self.logger.warning("buyingpower lower than available cash (short sells?), reducting cash for trades to buyingpower")
				return float(self.buyingpower[currency])
			else:
				return float(self.available_cash[currency])
		else:
			return 0


	def get_net_liquidation_value(self, currency = "USD"):
		#self.__updatePortfolioEvent.wait()
		sleep(5)
		if currency in self.net_liquidation:
			return float(self.net_liquidation[currency])
		else:
			return 0

	def handler(self,msg):
		self.logger.debug(msg)

	def update_comissions(self,timeout=60):
		self.logger.info("update_comissions")

		#first, reset commission data not not count it twice
		for i in self.log.keys():
			self.log[i].commission = None


		self.__SyncExecDetailsEvent.clear()
		self.__SyncExecDetailsReqID = self.NextOrderID
		self.NextOrderID += 1
		self.con.reqExecutions(self.__SyncExecDetailsReqID,ExecutionFilter())
		self.__SyncExecDetailsEvent.wait(timeout=timeout)
		

	def execDetailsHandler(self,msg):
		self.logger.debug(msg)
		self.logger.debug("execid %s, orderid %s",msg.execution.m_execId,msg.execution.m_orderId)
		self.__MapExecIdtoOrderId[msg.execution.m_execId] = msg.execution.m_orderId

	def execDetailsEndHandler(self,msg):
		self.logger.info(msg)
		if msg.reqId == self.__SyncExecDetailsReqID:
			self.__SyncExecDetailsEvent.set()

	def commissionReportHandler(self,msg):
		self.logger.debug(msg)
		if msg.commissionReport.m_execId in self.__MapExecIdtoOrderId:
			orderid = self.__MapExecIdtoOrderId[msg.commissionReport.m_execId]
			if orderid in self.log:
				if self.log[orderid].commission == None:
					self.log[orderid].commission = 0
				self.log[orderid].commission += msg.commissionReport.m_commission


	def updatePortfolioHandler(self,msg):
		self.logger.debug(msg.contract.m_symbol)
		self.logger.debug(msg)
		self.__SyncStockGetEvent.clear()
		if msg.position == 0: #TODO only works for long positions and stocks now
			if msg.contract.m_symbol in self.__portfolio:
				del self.__portfolio[msg.contract.m_symbol]
		else:
			self.__portfolio[msg.contract.m_symbol] = msg

	def accountDownloadEndHandler(self,msg):
		self.logger.debug(msg)
		self.__updatePortfolioEvent.set()

	def orderStatusHandler(self,msg):
		self.logger.debug(msg)
		self.logger.info("mxIBhandler: %s orders open", len(self.openorders))
		if (msg.status == "Filled") and msg.orderId in self.__MapToOriginalOrderID and self.__MapToOriginalOrderID[msg.orderId] in self.openorders:
			OrigOrdierID = self.__MapToOriginalOrderID[msg.orderId]
			self.log[OrigOrdierID].avg_fill_price = msg.avgFillPrice
			self.log[OrigOrdierID].fill_date = datetime.datetime.today().isoformat()
			if self.openorders[OrigOrdierID].market_data_subscribed == True:
				self.con.cancelMktData(OrigOrdierID)
			del self.openorders[OrigOrdierID]
		if (msg.status == "Cancelled") and msg.orderId in self.__MapToOriginalOrderID and self.__MapToOriginalOrderID[msg.orderId] in self.openorders:
			OrigOrdierID = self.__MapToOriginalOrderID[msg.orderId]
			self.log[OrigOrdierID].avg_fill_price = 0
			self.log[OrigOrdierID].canceled = True
			self.logger.warning("mxIBhandler WARNING! order id %s was canceled by IB",msg.orderId)
			if self.openorders[OrigOrdierID].market_data_subscribed == True:
				self.con.cancelMktData(OrigOrdierID)
			del self.openorders[OrigOrdierID]
		if len(self.openorders) == 0:
			self.__OrdersFinished.set()


	def nextValidIdHandler(self,msg):
		self.logger.debug(msg)
		self.NextOrderID = msg.orderId
	
	def errorHandler(self,msg):
		self.logger.debug(msg)
		if msg.errorCode==2110 or msg.errorCode == 2105:
			self.__connected = False
			self.logger.error(msg)
		elif msg.errorCode==200:
			if msg.id in self.__SyncStockGetMultipleIDs:
				msg.errorMsg += " ("+str(self.__SyncStockGetMultipleIDs[msg.id])+")" #add symbol
			self.logger.error(msg)
			if msg.id in self.__SyncStockGetMultipleIDs.keys():
				self.__SyncStockGetMultiplePrice[self.__SyncStockGetMultipleIDs[msg.id]] = 0
				del self.__SyncStockGetMultipleIDs[msg.id]
		elif msg.errorCode==2104 or msg.errorCode==2106 or msg.errorCode==2119:
			self.logger.info(msg)
		elif msg.errorCode==2107 or msg.errorCode==2108 or msg.errorCode==2109 or msg.errorCode==2110 or msg.errorCode==2100 or msg.errorCode==1102:
			self.logger.warning(msg)
		else:
			self.logger.error(msg)



	#symchonous functions that gets the current stock price from nyse
	def getprice(self,sym,timeout=10, pricetype="BID",primExch = ''):
		self.__SyncStockGetPrice = None #reset
		if pricetype == "ASK":
			self.__SyncStockGetType = TickTypeAsk
		elif pricetype == "BID":
			self.__SyncStockGetType = TickTypeBid
		else:
			self.logger.error("getprice: pricetype %s not understood",pricetype)
			sys.exit()


		contract = makeStkContract(sym)
		contract.m_primaryExch= primExch
		self.__SyncStockGetOrderID = self.NextOrderID
		self.con.reqMktData(self.__SyncStockGetOrderID, contract, '', True) #only request snapshot
		self.__SyncStockGetEvent.clear()
		if not self.__SyncStockGetEvent.wait(timeout=timeout):
			self.logger.error("getprice: timeout")
		self.NextOrderID += 1

		ret = self.__SyncStockGetPrice
		self.__SyncStockGetPrice = None
		return ret

	#synchonous functions that gets the current stock price from nyse
	# primExchDict = Optional parameter. Dict of Symbils -> Primary Exchange. To avoid ambiguous requests 
	def getprices(self,symbols,timeout=10,maxdatalines = 50, pricetype="BID",primExchDict = None):
		self.__SyncStockGetMultiplePrice = dict() #reset
		self.__SyncStockGetMultipleIDs = dict()
		
		if pricetype == "ASK":
			self.__SyncStockGetMultipleType = TickTypeAsk
		elif pricetype == "BID":
			self.__SyncStockGetMultipleType = TickTypeBid
		else:
			self.logger.error("getprices: pricetype %s not understood",pricetype)
			sys.exit()

		print("Fetching current prices...")
		for i in xrange(0, len(symbols), maxdatalines):
			_symbols = symbols[i:i+maxdatalines]
			self.__SyncStockGetMultipleEvent.clear()
			#it's safer to do it in two loops
			for k in _symbols:
				self.__SyncStockGetMultipleIDs[self.NextOrderID] = k #reset
				self.NextOrderID += 1


			copy = self.__SyncStockGetMultipleIDs.copy()
			for ID in copy:
				contract = makeStkContract(copy[ID])
				if primExchDict is not None and copy[ID] in primExchDict:
					contract.m_primaryExch = primExchDict[copy[ID]]
				self.con.reqMktData(ID, contract, '', True) #request snapshot
				self.logger.debug("getprices: requesting current price for %s with ticker id %s", copy[ID], ID)

			self.__SyncStockGetMultipleEvent.wait(timeout=4)
			PrintProgress(i,len(symbols))

			sleep(1)
		self.logger.info("getprices: finished fetching current prices")
		
		return self.__SyncStockGetMultiplePrice
		


	def tickPriceHandler(self,msg):
		self.logger.debug(msg)
		
		if msg.tickerId in self.__MapToOriginalOrderID and self.__MapToOriginalOrderID[msg.tickerId] in self.log:
			OriginalTickerID = self.__MapToOriginalOrderID[msg.tickerId]
			if msg.field == TickTypeBid:
				self.log[OriginalTickerID].bid = msg.price
			elif msg.field == TickTypeAsk:
				self.log[OriginalTickerID].ask = msg.price
			elif msg.field == TickTypeLast:
				self.log[OriginalTickerID].last = msg.price
			elif msg.field == TickTypeHigh:
				self.log[OriginalTickerID].high = msg.price
			elif msg.field == TickTypeLow:
				self.log[OriginalTickerID].low = msg.price
			elif msg.field == TickTypeClose:
				self.log[OriginalTickerID].close = msg.price
		#else:
		#	print  "tickPriceHandler: ticker id", msg.tickerId, "not in logs"
#		elif msg.tickerId != self.__SyncStockGetOrderID and msg.tickerId not in self.__SyncStockGetMultipleIDs:
#			print "tickPriceHandler: ticker with id", msg.tickerId, "not in logs"


		if msg.tickerId in self.__MapToOriginalOrderID:
			OrigOrdierID = self.__MapToOriginalOrderID[msg.tickerId]
			if OrigOrdierID in self.openorders:
				if self.openorders[OrigOrdierID].ordertype == "MKT":
					if (msg.field == TickTypeBid and self.openorders[OrigOrdierID].action == "SELL") or (msg.field == TickTypeAsk and self.openorders[OrigOrdierID].action == "BUY"):
						if (self.openorders[OrigOrdierID].action == "SELL" and msg.price >= self.openorders[OrigOrdierID].limitprice) or (self.openorders[OrigOrdierID].action == "BUY" and (msg.price <= self.openorders[OrigOrdierID].limitprice or self.openorders[OrigOrdierID].limitprice < 0)):
							if self.openorders[OrigOrdierID].prev_close == None or ((self.openorders[OrigOrdierID].prev_close/float(msg.price) < 2.) and (self.openorders[OrigOrdierID].prev_close/float(msg.price) > 0.5)):
								self.logger.warning("tickPriceHandler: not buying/selling %s consitency check failed (split?) prev_close %s current price %s" , self.openorders[OrigOrdierID].contract.m_symbol,self.openorders[OrigOrdierID].prev_close,msg.price) 
								del self.openorders[OrigOrdierID]
							elif msg.field == TickTypeBidSize and msg.size < self.openorders[OrigOrdierID].vollume:
								self.logger.debug("tickPriceHandler: not buying/selling %s vollume is too low", self.openorders[OrigOrdierID].contract.m_symbol)
								del self.openorders[OrigOrdierID]
							else:
								contract = self.openorders[OrigOrdierID].contract
								order = makeStkOrder(self.openorders[OrigOrdierID].vollume, self.openorders[OrigOrdierID].action, self._account, ordertype=self.openorders[OrigOrdierID].ordertype)
								self.con.placeOrder(self.NextOrderID,contract,order)
								self.__MapToOriginalOrderID[self.NextOrderID] = OrigOrdierID
								self.NextOrderID += 1
								self.__OrdersFinished.clear()
								self.logger.info("tickPriceHandler: placing order %s", self.openorders[OrigOrdierID])
						else:
							self.logger.warning("tickPriceHandler: not buying/selling %s targeted_price %s action: %s", self.openorders[OrigOrdierID].contract.m_symbol, self.openorders[OrigOrdierID].limitprice, self.openorders[OrigOrdierID].action)
				
				elif self.openorders[OrigOrdierID].ordertype == "LMT":
					if msg.field == TickTypeBid:
						self.openorders[OrigOrdierID].bid = msg.price
					elif msg.field == TickTypeAsk:
						self.openorders[OrigOrdierID].ask = msg.price

					if self.openorders[OrigOrdierID].ask is not None and self.openorders[OrigOrdierID].ask is not None:
						#calculate midpoint in case ba_offset was set
						midpoint = self._calc_midpoint(self.openorders[OrigOrdierID].bid,self.openorders[OrigOrdierID].ask,self.openorders[OrigOrdierID].ba_offset,self.openorders[OrigOrdierID].action,oderid=OrigOrdierID)

						#now check if the order can be placed
						if midpoint is not None:
							if self.openorders[OrigOrdierID].action == "BUY" and self.openorders[OrigOrdierID].bid is not None and (self.openorders[OrigOrdierID].limitprice <= msg.price or self.openorders[OrigOrdierID].limitprice < 0) and self.openorders[OrigOrdierID].placed is not True:
								contract = self.openorders[OrigOrdierID].contract
								order = makeStkOrder(self.openorders[OrigOrdierID].vollume, self.openorders[OrigOrdierID].action, self._account, ordertype=self.openorders[OrigOrdierID].ordertype)
								order.m_lmtPrice = midpoint
								self.openorders[OrigOrdierID].limitprice = order.m_lmtPrice

								neworderid = self.NextOrderID
								self.NextOrderID += 1
								self.__MapToOriginalOrderID[neworderid] = OrigOrdierID
								self.__MapToExecuteOrderID[OrigOrdierID] = neworderid
								self.con.placeOrder(neworderid,contract,order)

								self.openorders[OrigOrdierID].placed = True
								self.openorders[OrigOrdierID].placed_date = datetime.datetime.today()
								self.openorders[OrigOrdierID].last_adjust = self.openorders[OrigOrdierID].placed_date
								if self.adjist_limits_thread is None:
									self.adjist_limits_thread = Timer(self.limit_adjust_interval, self.adjust_limits, ()).start()

								self.logger.info("tickPriceHandler: placing order %s midpoint%s %s ba_offset:%s bid:%s ask:%s", self.openorders[OrigOrdierID], midpoint, order.m_lmtPrice, self.openorders[OrigOrdierID].ba_offset, self.openorders[OrigOrdierID].bid, self.openorders[OrigOrdierID].ask)
							elif self.openorders[OrigOrdierID].action == "SELL" and self.openorders[OrigOrdierID].ask is not None and (self.openorders[OrigOrdierID].limitprice >= msg.price or self.openorders[OrigOrdierID].limitprice < 0) and self.openorders[OrigOrdierID].placed is not True:
								contract = self.openorders[OrigOrdierID].contract
								order = makeStkOrder(self.openorders[OrigOrdierID].vollume, self.openorders[OrigOrdierID].action, self._account, ordertype=self.openorders[OrigOrdierID].ordertype)
								order.m_lmtPrice = midpoint
								self.openorders[OrigOrdierID].limitprice = order.m_lmtPrice
								
								neworderid = self.NextOrderID
								self.NextOrderID += 1
								self.__MapToOriginalOrderID[neworderid] = OrigOrdierID
								self.__MapToExecuteOrderID[OrigOrdierID] = neworderid
								self.con.placeOrder(neworderid,contract,order)

								self.openorders[OrigOrdierID].placed = True
								self.openorders[OrigOrdierID].placed_date = datetime.datetime.today()
								self.openorders[OrigOrdierID].last_adjust = self.openorders[OrigOrdierID].placed_date
								if self.adjist_limits_thread is None:
									self.adjist_limits_thread = Timer(self.limit_adjust_interval, self.adjust_limits, ()).start()

								self.logger.info("tickPriceHandler: placing order %s midpoint:%s %s ba_offset:%s bid:%s ask:%s", self.openorders[OrigOrdierID], midpoint, order.m_lmtPrice, self.openorders[OrigOrdierID].ba_offset, self.openorders[OrigOrdierID].bid, self.openorders[OrigOrdierID].ask)

				else:
					self.logger.error("tickPriceHandler: unknown order type %s", self.openorders[OrigOrdierID].ordertype)
					sys.exit()
		#synchronous price request?
		elif msg.tickerId == self.__SyncStockGetOrderID:
			if msg.field == self.__SyncStockGetType:
				self.__SyncStockGetPrice = msg.price
				self.__SyncStockGetEvent.set()
		#synchronous multiple price request
		elif msg.tickerId in self.__SyncStockGetMultipleIDs:
			if msg.field == self.__SyncStockGetMultipleType:
				self.__SyncStockGetMultiplePrice[self.__SyncStockGetMultipleIDs[msg.tickerId]] = msg.price
				del self.__SyncStockGetMultipleIDs[msg.tickerId]
				if len(self.__SyncStockGetMultipleIDs) == 0:
					self.__SyncStockGetMultipleEvent.set()
#		else:
#			print "tickPriceHandler: not buying/selling order with id ", msg.tickerId, "not in openorders and getprice was not called"


	def _calc_midpoint(self,bid,ask,ba_offset,action,oderid="[no orderid specified]"):
		if bid is None or ask is None or ba_offset is None or action is None:
			self.logger.error("ERROR %s insufficient data specified (bid/ask/ba_spread/action missing?)",oderid)
			return None
		else:
			spread = ask - bid
			offset = spread * ba_offset

			if ask == 0 or spread/ask > 0.1:
				self.logger.warning("WANRING %s spread bigger than 10 percent. Not placing order", oderid)
				return None
			else:
				if action == "BUY":
					return round(ask - offset - 0.004,2)
				elif action == "SELL":
					return round(bid + offset + 0.004,2)
				else:
					return None



	def tickSizeHandler(self,msg):
		self.logger.debug(msg)
		if msg.tickerId in self.__MapToOriginalOrderID and self.__MapToOriginalOrderID[msg.tickerId] in self.log:
			OriginalTickerID = self.__MapToOriginalOrderID[msg.tickerId]
			if msg.field == TickTypeBidSize:
				self.log[OriginalTickerID].bidsize = msg.size
			elif msg.field == TickTypeAskSize:
				self.log[OriginalTickerID].asksize = msg.size
			elif msg.field == TickTypeLastSize:
				self.log[OriginalTickerID].lastsize = msg.size
			elif msg.field == TickTypeVollume:
				self.log[OriginalTickerID].vollume = msg.size
#		elif msg.tickerId != self.__SyncStockGetOrderID and msg.tickerId not in self.__SyncStockGetMultipleIDs:
#			print "tickSizeHandler: ticker with id", msg.tickerId, "not in logs"


	def updateAccountValueHandler(self,msg):
		self.logger.debug(msg)
		if msg.key == "CashBalance":
			self.available_cash[msg.currency] = msg.value
		if msg.key == "BuyingPower":
			self.buyingpower[msg.currency] = msg.value
		if msg.key == "NetLiquidationByCurrency":
			self.net_liquidation[msg.currency] = msg.value

	#get price info, then make market order if price is acceptable regarding the limitprice
	def place_order_quote(self,contract,vollume,price,limitprice,action, unique_ID = "", prev_close = None):
		if unique_ID == "":
			unique_ID = current_milli_time
		orderid = self.NextOrderID
		self.NextOrderID += 1
		self.openorders[orderid] = OpenOrder(contract,vollume,price,limitprice,action)
		self.openorders[orderid].ordertype = "MKT"
		self.openorders[orderid].prev_close = prev_close
		
		self.__MapToOriginalOrderID[orderid] = orderid
		self.con.reqMktData(orderid, contract, '', True) #only request snapshot

		self.log[orderid] = LogEntry(timestamp = datetime.datetime.today().isoformat(), symbol=contract.m_symbol, ordervollume = vollume, targeted_price = -1, limitprice = limitprice, unique_ID = unique_ID)
		self.log[orderid].action = action
		self.log[orderid].ordertype = self.openorders[orderid].ordertype

		self.logger.info("%s %s %s %s vollume: %s targeting price: %s limit: %s %s", orderid, unique_ID, action, contract.m_symbol, vollume, price, limitprice, self.openorders[orderid].ordertype)

		sleep(2)

		return orderid

	#ba_offset: percantage of ba spread. eg. ask = 1.10 USD bid = 1.00 USD, ba_offset = 0.1 -> place limit order SELL at 1.09 USD
	#limitprice: hard limit: don't buy or sell if over or under this price
	def place_limitorder_quote(self,contract,vollume,limitprice,action, unique_ID = "", ba_offset = 0):
		if unique_ID == "":
			unique_ID = current_milli_time
		orderid = self.NextOrderID
		self.NextOrderID += 1

		self.openorders[orderid] = OpenOrder(contract,vollume,None,limitprice,action) #limitprice -1 -> check bid ask and place limit there
		self.openorders[orderid].ordertype = "LMT"
		self.openorders[orderid].ba_offset = ba_offset
		self.openorders[orderid].adjust_periodical = True
		self.openorders[orderid].market_data_subscribed = True

		self.__MapToOriginalOrderID[orderid] = orderid

		self.logger.info("%s market data for order triggered", orderid)
		self.con.reqMktData(orderid, contract, '', False) #only for log

		self.log[orderid] = LogEntry(timestamp = datetime.datetime.today().isoformat(), symbol=contract.m_symbol, ordervollume = vollume, targeted_price = -1, limitprice = limitprice, unique_ID = unique_ID)
		self.log[orderid].action = action
		self.log[orderid].ba_offset = ba_offset
		self.log[orderid].ordertype = "LMT"
		self.__OrdersFinished.clear()
		return orderid


	def place_limitorder(self,contract,vollume,limitprice,action, unique_ID=""):
		if unique_ID == "":
			unique_ID = current_milli_time

		orderid = self.NextOrderID
		self.NextOrderID += 1

		self.openorders[orderid] = OpenOrder(contract,vollume,None,limitprice,action) #limitprice -1 -> check bid ask and place limit there
		self.openorders[orderid].ordertype = "LMT"
		self.openorders[orderid].adjust_periodical = False
		self.openorders[orderid].market_data_subscribed = False

		order = makeStkOrder(vollume, action, self._account, ordertype="LMT")

		order.m_lmtPrice = limitprice

		self.__MapToOriginalOrderID[orderid] = orderid
		self.con.reqMktData(orderid, contract, '', True) #only for log

		self.con.placeOrder(orderid ,contract,order)
		self.__OrdersFinished.clear()

		self.log[orderid] = LogEntry(timestamp = datetime.datetime.today().isoformat(), symbol=contract.m_symbol, ordervollume = vollume, targeted_price = price, limitprice = limitprice)
		self.log[orderid].action = action
		self.log[orderid].ordertype = "LMT"

		return orderid


	def place_peggedmid_order(self,contract,vollume,limitprice,action, unique_ID=""):
		if unique_ID == "":
			unique_ID = current_milli_time
		orderid = self.NextOrderID
		self.NextOrderID += 1
		order = makeStkOrder(vollume, action, self._account, ordertype="PEGMID")

		contract.m_exchange = 'ISLAND' #required for PEGMID
		order.m_lmtPrice = limitprice

		self.__MapToOriginalOrderID[orderid] = orderid
		self.con.reqMktData(orderid, contract, '', True) #only for log

		self.con.placeOrder(orderid ,contract,order)
		self.__OrdersFinished.clear()

		self.log[orderid] = LogEntry(timestamp = datetime.datetime.today().isoformat(), symbol=contract.m_symbol, ordervollume = vollume, targeted_price = 0, limitprice = limitprice)
		self.log[orderid].action = action
		self.log[orderid].ordertype = "PEGMID"

		return orderid


	#market on close
	def place_moc_order(self,contract,vollume,action, unique_ID=""):
		if unique_ID == "":
			unique_ID = current_milli_time
		orderid = self.NextOrderID
		self.NextOrderID += 1
		order = makeStkOrder(vollume, action, self._account, ordertype="MOC")

		self.__MapToOriginalOrderID[orderid] = orderid
		self.con.reqMktData(orderid, contract, '', True) #only for log

		self.con.placeOrder(orderid ,contract,order)
		self.__OrdersFinished.clear()

		self.log[orderid] = LogEntry(timestamp = datetime.datetime.today().isoformat(), symbol=contract.m_symbol, ordervollume = vollume, targeted_price = 0, limitprice = -1)
		self.log[orderid].action = action
		self.log[orderid].ordertype = "MOC"


	def adjust_limits(self):
		self.logger.info("adjust_limits: checking midpoint limits")
		current_time = datetime.datetime.today()
		for orderid in self.openorders.keys():
			if self.openorders[orderid].adjust_periodical == True and orderid in self.__MapToExecuteOrderID:
				if (current_time - self.openorders[orderid].last_adjust).total_seconds() > self.limit_adjust_interval*0.9:

					contract = self.openorders[orderid].contract
					if (current_time - self.openorders[orderid].placed_date) >= self.max_adjust_time:
						self.openorders[orderid].ba_offset = 0
						self.logger.info("adjust_limits: %s setting ba_offset to zero because max_adjust_time as passed",self.openorders[orderid].contract.m_symbol)

					midpoint = self._calc_midpoint(self.openorders[orderid].bid,self.openorders[orderid].ask,self.openorders[orderid].ba_offset,self.openorders[orderid].action,oderid=orderid)
					self.logger.debug("midpoint %s", midpoint)
					if midpoint is not None:

						order = makeStkOrder(self.openorders[orderid].vollume, self.openorders[orderid].action, self._account, ordertype=self.openorders[orderid].ordertype)
						order.m_lmtPrice = midpoint

						exec_orderid = self.__MapToExecuteOrderID[orderid]
						self.con.placeOrder(exec_orderid,contract,order)
						self.openorders[orderid].last_adjust = datetime.datetime.today()
						self.openorders[orderid].limitprice = order.m_lmtPrice
						self.logger.info("adjust_limits: Updated %s order with id %s to %s (bid: %s / ask: %s)",self.openorders[orderid].contract.m_symbol, orderid, order.m_lmtPrice, self.openorders[orderid].bid, self.openorders[orderid].ask)

		#reschedule the timer
		if len(self.openorders.keys()) > 0:
			self.adjist_limits_thread = Timer(self.limit_adjust_interval, self.adjust_limits, ()).start()
		else:
			self.adjist_limits_thread = None


	def release(self):
		self.logger.info("Exiting mxIBhandler")
		self.logger.info("open orders")
		for k in self.openorders.keys():
			self.logger.info("%s %s", k, self.openorders[k])
		self.con.unregisterAll(1)
		if self.adjist_limits_thread is not None:
			self.adjist_limits_thread.cancel()
		self.logger.info("Closing connection...")
		sleep(10)
		self.con.disconnect()
        
